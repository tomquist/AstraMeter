//! TCP + UDP using blocking `std::net` sockets wrapped in
//! `tokio::task::spawn_blocking`.
//!
//! Tokio's IO driver (mio → epoll) can't initialise on `esp-idf` —
//! `tokio::runtime::Builder::enable_io()` returns
//! `Permission denied (os error 13)`. The runtime is therefore built
//! with `enable_time()` only on this target, which means
//! `tokio::net::{TcpStream, UdpSocket}` aren't usable. Fall back to
//! blocking `std::net::*` for the actual syscalls and dispatch via
//! `spawn_blocking` so the current-thread runtime keeps making
//! progress on other tasks.

use astrameter_platform::net::{
    NetError, TcpConnect, TcpStream as PlatformTcpStream, UdpBind, UdpSocket,
};
use async_trait::async_trait;
use parking_lot::Mutex as PMutex;
use std::future::Future;
use std::io;
use std::net::{Ipv4Addr, SocketAddr, TcpStream as StdTcp, UdpSocket as StdUdp};
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

pub struct TokioUdpBind;

struct BlockingUdp(Arc<StdUdp>);

#[async_trait]
impl UdpSocket for BlockingUdp {
    async fn send_to(&self, buf: &[u8], target: SocketAddr) -> Result<usize, NetError> {
        let sock = self.0.clone();
        let buf = buf.to_vec();
        tokio::task::spawn_blocking(move || sock.send_to(&buf, target))
            .await
            .map_err(|e| NetError::Send(format!("join: {e}")))?
            .map_err(|e| NetError::Send(e.to_string()))
    }

    async fn recv_from(&self, buf: &mut [u8]) -> Result<(usize, SocketAddr), NetError> {
        let sock = self.0.clone();
        let cap = buf.len();
        let (got_n, from, data) = tokio::task::spawn_blocking(move || {
            let mut inner = vec![0u8; cap];
            let (n, from) = sock.recv_from(&mut inner)?;
            inner.truncate(n);
            std::io::Result::Ok((n, from, inner))
        })
        .await
        .map_err(|e| NetError::Recv(format!("join: {e}")))?
        .map_err(|e| NetError::Recv(e.to_string()))?;
        buf[..got_n].copy_from_slice(&data);
        Ok((got_n, from))
    }
}

#[async_trait]
impl UdpBind for TokioUdpBind {
    async fn bind(&self, addr: SocketAddr) -> Result<Box<dyn UdpSocket>, NetError> {
        let sock = tokio::task::spawn_blocking(move || StdUdp::bind(addr))
            .await
            .map_err(|e| NetError::Bind(format!("join: {e}")))?
            .map_err(|e| NetError::Bind(e.to_string()))?;
        // Blocking timeouts let `recv_from` wake periodically so the
        // outer cancellation tokens get a chance to fire even on quiet
        // links.
        let _ = sock.set_read_timeout(Some(std::time::Duration::from_secs(1)));
        Ok(Box::new(BlockingUdp(Arc::new(sock))))
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
        Ok(Box::new(BlockingUdp(Arc::new(sock))))
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
