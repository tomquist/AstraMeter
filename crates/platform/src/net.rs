//! TCP connect + UDP traits (including multicast join for SMA Speedwire).

use async_trait::async_trait;
use std::net::SocketAddr;
use tokio::io::{AsyncRead, AsyncWrite};

#[derive(Debug, thiserror::Error)]
pub enum NetError {
    #[error("bind error: {0}")]
    Bind(String),
    #[error("connect error: {0}")]
    Connect(String),
    #[error("send error: {0}")]
    Send(String),
    #[error("recv error: {0}")]
    Recv(String),
    #[error("multicast join error: {0}")]
    JoinMulticast(String),
}

#[async_trait]
pub trait UdpSocket: Send + Sync {
    async fn send_to(&self, buf: &[u8], target: SocketAddr) -> Result<usize, NetError>;
    /// Returns (n_bytes, source_addr).
    async fn recv_from(&self, buf: &mut [u8]) -> Result<(usize, SocketAddr), NetError>;
}

#[async_trait]
pub trait UdpBind: Send + Sync {
    async fn bind(&self, addr: SocketAddr) -> Result<Box<dyn UdpSocket>, NetError>;
    /// Bind and join an IPv4 multicast group. `interface` is the local
    /// interface address (`0.0.0.0` to pick the default route).
    async fn bind_multicast(
        &self,
        addr: SocketAddr,
        group: std::net::Ipv4Addr,
        interface: std::net::Ipv4Addr,
    ) -> Result<Box<dyn UdpSocket>, NetError>;
}

/// TCP connect for protocols (Modbus, raw clients) that need a stream.
#[async_trait]
pub trait TcpConnect: Send + Sync {
    async fn connect(&self, addr: SocketAddr) -> Result<TcpStream, NetError>;
}

pub type TcpStream = Box<dyn TcpStreamLike>;

pub trait TcpStreamLike: AsyncRead + AsyncWrite + Send + Unpin {}

impl<T: AsyncRead + AsyncWrite + Send + Unpin> TcpStreamLike for T {}
