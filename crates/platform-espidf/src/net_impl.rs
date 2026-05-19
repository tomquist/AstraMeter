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
use std::net::{Ipv4Addr, SocketAddr, UdpSocket as StdUdp};
use std::sync::Arc;

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
        // Returning a usable async TcpStream that works without tokio's
        // IO driver requires a fairly involved wrapper; powermeters that
        // need raw TCP (modbus-tcp on ESP32) are out of scope until we
        // route Modbus through esp-idf-svc's transport too.
        let _ = addr;
        Err(NetError::Connect(
            "TcpConnect not supported on ESP32 yet — tokio's IO driver is disabled and \
             the trait returns a tokio::net::TcpStream. Modbus-TCP isn't wired through \
             esp-idf-svc yet."
                .to_string(),
        ))
    }
}
