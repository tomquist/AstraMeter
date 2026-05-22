//! TCP + UDP using blocking `std::net` sockets.
//!
//! Tokio's IO driver (mio → epoll) can't initialise on `esp-idf` —
//! `tokio::runtime::Builder::enable_io()` returns
//! `Permission denied (os error 13)`. The runtime is therefore built
//! with `enable_time()` only on this target, which means
//! `tokio::net::{TcpStream, UdpSocket}` aren't usable.
//!
//! `send_to` and the TCP wrapper dispatch via `spawn_blocking` (the
//! tokio blocking pool). UDP `recv_from` is special: it's a
//! long-lived consumer (the CT002 emulator's recv loop blocks here
//! for every UDP poll from a Marstek battery), and pinning a
//! pthread permanently to internal SRAM via tokio's pool eventually
//! exhausts the internal heap as it fragments around the
//! permanently-held slot. So `bind` spawns a dedicated FreeRTOS
//! task whose stack lives in PSRAM (`xTaskCreatePinnedToCoreWithCaps`
//! with `MALLOC_CAP_SPIRAM`), and that task pushes received packets
//! into a tokio mpsc channel that `recv_from` awaits. No internal
//! SRAM is committed for the recv path at all.

use astrameter_platform::net::{
    NetError, TcpConnect, TcpStream as PlatformTcpStream, UdpBind, UdpSocket,
};
use async_trait::async_trait;
use parking_lot::Mutex as PMutex;
use std::future::Future;
use std::io;
use std::net::{Ipv4Addr, SocketAddr, TcpStream as StdTcp, UdpSocket as StdUdp};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::sync::Mutex as AsyncMutex;

pub struct TokioUdpBind;

/// One packet received by the dedicated recv task.
struct RxPacket {
    data: Vec<u8>,
    from: SocketAddr,
}

struct BlockingUdp {
    /// Cloned for `send_to` so multiple sends can run concurrently
    /// without contending with the recv task.
    sock: Arc<StdUdp>,
    /// Owned by `recv_from`. `AsyncMutex` because awaiting from
    /// the receiver across `.await` boundaries needs to hold the
    /// guard across `.await`s.
    rx: AsyncMutex<tokio::sync::mpsc::UnboundedReceiver<RxPacket>>,
    /// Signals the recv task to exit on Drop.
    cancel: Arc<AtomicBool>,
}

impl Drop for BlockingUdp {
    fn drop(&mut self) {
        self.cancel.store(true, Ordering::SeqCst);
        // The recv task picks up the flag on its next 1 s
        // `recv_from` timeout and self-deletes. We don't join it —
        // the OS reclaims the PSRAM stack via the idle task.
    }
}

#[async_trait]
impl UdpSocket for BlockingUdp {
    async fn send_to(&self, buf: &[u8], target: SocketAddr) -> Result<usize, NetError> {
        // Call `std::net::UdpSocket::send_to` directly rather than
        // bouncing through `tokio::task::spawn_blocking`. UDP sends
        // are non-blocking on lwIP — the datagram is handed to the
        // TCP/IP stack and the syscall returns in microseconds. With
        // 2 Marstek batteries polling every ~1 s, the previous
        // spawn_blocking path was generating ~7,200 blocking-pool
        // task allocations/hour, the resulting Task-struct churn
        // intermittently corrupted tokio's task state machine, and
        // we kept hitting `prev.is_running()` /
        // `next.is_notified()` assertion panics in
        // `transition_to_complete`. Calling sync send_to inline
        // removes that churn entirely.
        self.sock
            .send_to(buf, target)
            .map_err(|e| NetError::Send(e.to_string()))
    }

    async fn recv_from(&self, buf: &mut [u8]) -> Result<(usize, SocketAddr), NetError> {
        let mut rx = self.rx.lock().await;
        let pkt = rx
            .recv()
            .await
            .ok_or_else(|| NetError::Recv("recv task ended (channel closed)".to_string()))?;
        let n = pkt.data.len().min(buf.len());
        buf[..n].copy_from_slice(&pkt.data[..n]);
        Ok((n, pkt.from))
    }
}

/// Spawn the long-lived recv task on a PSRAM-stack FreeRTOS task
/// and return a `BlockingUdp` wired to its output channel.
fn spawn_recv_task(sock: Arc<StdUdp>) -> Result<BlockingUdp, NetError> {
    use esp_idf_svc::sys;

    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<RxPacket>();
    let cancel = Arc::new(AtomicBool::new(false));

    struct Args {
        sock: Arc<StdUdp>,
        tx: tokio::sync::mpsc::UnboundedSender<RxPacket>,
        cancel: Arc<AtomicBool>,
    }

    extern "C" fn task_entry(arg: *mut std::ffi::c_void) {
        // SAFETY: `arg` is the `Box::into_raw` pointer the spawner
        // handed us. We take ownership and the box is dropped at
        // end of scope.
        let args: Box<Args> = unsafe { Box::from_raw(arg as *mut Args) };
        let Args { sock, tx, cancel } = *args;

        let mut buf = vec![0u8; 4096];
        loop {
            if cancel.load(Ordering::SeqCst) {
                break;
            }
            match sock.recv_from(&mut buf) {
                Ok((n, from)) => {
                    let pkt = RxPacket {
                        data: buf[..n].to_vec(),
                        from,
                    };
                    if tx.send(pkt).is_err() {
                        // Receiver dropped — caller is gone. Exit.
                        break;
                    }
                }
                Err(e) => {
                    // 1 s read timeout fires constantly when nothing
                    // is arriving — silent. Log only real failures.
                    let kind = e.kind();
                    let is_timeout =
                        matches!(kind, io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut);
                    if !is_timeout {
                        log::warn!("net_impl: udp recv task: {e}");
                    }
                }
            }
        }
        // SAFETY: `NULL` to `vTaskDelete` deletes the calling task;
        // the IDF idle task reclaims our PSRAM stack.
        unsafe { sys::vTaskDelete(std::ptr::null_mut()) };
    }

    // Stash a second Arc clone for `BlockingUdp::send_to` BEFORE
    // moving the original into the task's Args.
    let sock_for_send = sock.clone();
    let args = Box::new(Args {
        sock,
        tx,
        cancel: cancel.clone(),
    });
    let arg_ptr = Box::into_raw(args) as *mut std::ffi::c_void;

    let mut handle: sys::TaskHandle_t = std::ptr::null_mut();
    let rc = unsafe {
        sys::xTaskCreatePinnedToCoreWithCaps(
            Some(task_entry),
            b"udp-recv\0".as_ptr() as *const _,
            // 12 KiB is plenty for `std::net::UdpSocket::recv_from`
            // (lwIP recv + newlib syscall stubs). Lives in PSRAM so
            // the size has no internal-SRAM cost.
            12 * 1024,
            arg_ptr,
            5,
            &mut handle,
            // `tskNO_AFFINITY` — bindgen mangles it across IDF
            // versions, so use the literal directly.
            0x7FFF_FFFFi32,
            sys::MALLOC_CAP_SPIRAM | sys::MALLOC_CAP_8BIT,
        )
    };
    if rc != 1 {
        unsafe { drop(Box::from_raw(arg_ptr as *mut Args)) };
        return Err(NetError::Bind(format!(
            "xTaskCreatePinnedToCoreWithCaps(udp-recv): rc={rc}"
        )));
    }

    Ok(BlockingUdp {
        sock: sock_for_send,
        rx: AsyncMutex::new(rx),
        cancel,
    })
}

#[async_trait]
impl UdpBind for TokioUdpBind {
    async fn bind(&self, addr: SocketAddr) -> Result<Box<dyn UdpSocket>, NetError> {
        let sock = tokio::task::spawn_blocking(move || StdUdp::bind(addr))
            .await
            .map_err(|e| NetError::Bind(format!("join: {e}")))?
            .map_err(|e| NetError::Bind(e.to_string()))?;
        // 1 s read timeout lets the dedicated recv task observe the
        // cancellation flag periodically without blocking forever
        // on an idle socket.
        let _ = sock.set_read_timeout(Some(std::time::Duration::from_secs(1)));
        Ok(Box::new(spawn_recv_task(Arc::new(sock))?))
    }

    async fn bind_multicast(
        &self,
        addr: SocketAddr,
        group: Ipv4Addr,
        interface: Ipv4Addr,
    ) -> Result<Box<dyn UdpSocket>, NetError> {
        let sock = tokio::task::spawn_blocking(move || -> std::io::Result<StdUdp> {
            let s = socket2::Socket::new(
                socket2::Domain::IPV4,
                socket2::Type::DGRAM,
                Some(socket2::Protocol::UDP),
            )?;
            s.set_reuse_address(true)?;
            s.bind(&addr.into())?;
            s.join_multicast_v4(&group, &interface)?;
            Ok(s.into())
        })
        .await
        .map_err(|e| NetError::Bind(format!("join: {e}")))?
        .map_err(|e| NetError::Bind(e.to_string()))?;
        let _ = sock.set_read_timeout(Some(std::time::Duration::from_secs(1)));
        Ok(Box::new(spawn_recv_task(Arc::new(sock))?))
    }
}

pub struct TokioTcpConnect;

#[async_trait]
impl TcpConnect for TokioTcpConnect {
    async fn connect(&self, addr: SocketAddr) -> Result<PlatformTcpStream, NetError> {
        let stream = tokio::task::spawn_blocking(move || {
            // 10 s is generous for LAN Modbus brokers but still bounded.
            StdTcp::connect_timeout(&addr, std::time::Duration::from_secs(10))
        })
        .await
        .map_err(|e| NetError::Connect(format!("join: {e}")))?
        .map_err(|e| NetError::Connect(e.to_string()))?;
        // Per-call read timeouts let `poll_read` wake periodically so
        // higher layers can drop the stream on cancellation. 1 s is
        // long enough to keep Modbus round-trips snappy without spinning.
        let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(1)));
        let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(10)));
        Ok(Box::new(BlockingTcpStream::new(stream)))
    }
}

/// AsyncRead + AsyncWrite over a blocking `std::net::TcpStream`.
///
/// Each `poll_read` / `poll_write` dispatches a `spawn_blocking` task
/// that performs the syscall and parks the result. The next poll
/// drains the result back into the caller's buffer. The stream itself
/// lives behind an `Arc<Mutex<Option<...>>>` so the blocking task can
/// own it for the duration of the syscall and put it back when done.
///
/// This is heavier than tokio's IO driver but Modbus-TCP polls at
/// ~1 Hz with ~10-byte request/response payloads, so the per-syscall
/// `spawn_blocking` overhead is irrelevant. The IO driver itself is
/// unavailable on `esp-idf` (`Permission denied` when initialising
/// mio's epoll), so this is the only portable path.
pub(crate) struct BlockingTcpStream {
    stream: Arc<PMutex<Option<StdTcp>>>,
    read_in_flight: Option<tokio::task::JoinHandle<(StdTcp, io::Result<Vec<u8>>)>>,
    write_in_flight: Option<tokio::task::JoinHandle<(StdTcp, io::Result<usize>)>>,
    shutdown_in_flight: Option<tokio::task::JoinHandle<(StdTcp, io::Result<()>)>>,
}

impl std::fmt::Debug for BlockingTcpStream {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BlockingTcpStream").finish()
    }
}

impl BlockingTcpStream {
    fn new(stream: StdTcp) -> Self {
        Self {
            stream: Arc::new(PMutex::new(Some(stream))),
            read_in_flight: None,
            write_in_flight: None,
            shutdown_in_flight: None,
        }
    }

    fn take_stream(&self) -> io::Result<StdTcp> {
        self.stream
            .lock()
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotConnected, "stream busy or closed"))
    }
}

impl AsyncRead for BlockingTcpStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        loop {
            if let Some(handle) = self.read_in_flight.as_mut() {
                match Pin::new(handle).poll(cx) {
                    Poll::Pending => return Poll::Pending,
                    Poll::Ready(Err(e)) => {
                        self.read_in_flight = None;
                        return Poll::Ready(Err(io::Error::other(format!("join: {e}"))));
                    }
                    Poll::Ready(Ok((stream, res))) => {
                        self.read_in_flight = None;
                        *self.stream.lock() = Some(stream);
                        return match res {
                            Ok(data) if data.is_empty() => {
                                // 0-byte read on a blocking TCP socket
                                // means EOF only on `Read::read`; with
                                // the timeout we set, it's normally
                                // a timeout. Treat as Pending so
                                // higher-level reads keep waiting.
                                cx.waker().wake_by_ref();
                                Poll::Pending
                            }
                            Ok(data) => {
                                buf.put_slice(&data);
                                Poll::Ready(Ok(()))
                            }
                            Err(e)
                                if e.kind() == io::ErrorKind::WouldBlock
                                    || e.kind() == io::ErrorKind::TimedOut =>
                            {
                                cx.waker().wake_by_ref();
                                Poll::Pending
                            }
                            Err(e) => Poll::Ready(Err(e)),
                        };
                    }
                }
            }
            // No in-flight read — spawn one.
            let stream = match self.take_stream() {
                Ok(s) => s,
                Err(e) => return Poll::Ready(Err(e)),
            };
            let capacity = buf.remaining().min(2048);
            let handle = tokio::task::spawn_blocking(move || {
                use std::io::Read;
                let mut local = vec![0u8; capacity];
                let mut s = stream;
                let res = s.read(&mut local).map(|n| {
                    local.truncate(n);
                    local
                });
                (s, res)
            });
            self.read_in_flight = Some(handle);
        }
    }
}

impl AsyncWrite for BlockingTcpStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        loop {
            if let Some(handle) = self.write_in_flight.as_mut() {
                match Pin::new(handle).poll(cx) {
                    Poll::Pending => return Poll::Pending,
                    Poll::Ready(Err(e)) => {
                        self.write_in_flight = None;
                        return Poll::Ready(Err(io::Error::other(format!("join: {e}"))));
                    }
                    Poll::Ready(Ok((stream, res))) => {
                        self.write_in_flight = None;
                        *self.stream.lock() = Some(stream);
                        return Poll::Ready(res);
                    }
                }
            }
            let stream = match self.take_stream() {
                Ok(s) => s,
                Err(e) => return Poll::Ready(Err(e)),
            };
            let owned = data.to_vec();
            let handle = tokio::task::spawn_blocking(move || {
                use std::io::Write;
                let mut s = stream;
                let res = s.write(&owned);
                (s, res)
            });
            self.write_in_flight = Some(handle);
        }
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        // `std::net::TcpStream` writes are unbuffered.
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        loop {
            if let Some(handle) = self.shutdown_in_flight.as_mut() {
                match Pin::new(handle).poll(cx) {
                    Poll::Pending => return Poll::Pending,
                    Poll::Ready(Err(e)) => {
                        self.shutdown_in_flight = None;
                        return Poll::Ready(Err(io::Error::other(format!("join: {e}"))));
                    }
                    Poll::Ready(Ok((_stream, res))) => {
                        self.shutdown_in_flight = None;
                        // Don't put the stream back — we're done with it.
                        return Poll::Ready(res);
                    }
                }
            }
            let stream = match self.take_stream() {
                Ok(s) => s,
                Err(e) => return Poll::Ready(Err(e)),
            };
            let handle = tokio::task::spawn_blocking(move || {
                let s = stream;
                let res = s.shutdown(std::net::Shutdown::Both);
                (s, res)
            });
            self.shutdown_in_flight = Some(handle);
        }
    }
}
