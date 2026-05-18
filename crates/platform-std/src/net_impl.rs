use astrameter_platform::net::{NetError, TcpConnect, TcpStream, UdpBind, UdpSocket};
use async_trait::async_trait;
use std::net::{Ipv4Addr, SocketAddr};
use tokio::sync::Mutex;

pub struct TokioUdpBind;

struct TokioUdp(tokio::net::UdpSocket);

#[async_trait]
impl UdpSocket for TokioUdp {
    async fn send_to(&self, buf: &[u8], target: SocketAddr) -> Result<usize, NetError> {
        self.0
            .send_to(buf, target)
            .await
            .map_err(|e| NetError::Send(e.to_string()))
    }

    async fn recv_from(&self, buf: &mut [u8]) -> Result<(usize, SocketAddr), NetError> {
        self.0
            .recv_from(buf)
            .await
            .map_err(|e| NetError::Recv(e.to_string()))
    }
}

#[async_trait]
impl UdpBind for TokioUdpBind {
    async fn bind(&self, addr: SocketAddr) -> Result<Box<dyn UdpSocket>, NetError> {
        let sock = tokio::net::UdpSocket::bind(addr)
            .await
            .map_err(|e| NetError::Bind(e.to_string()))?;
        Ok(Box::new(TokioUdp(sock)))
    }

    async fn bind_multicast(
        &self,
        addr: SocketAddr,
        group: Ipv4Addr,
        interface: Ipv4Addr,
    ) -> Result<Box<dyn UdpSocket>, NetError> {
        let domain = socket2::Domain::IPV4;
        let socket =
            socket2::Socket::new(domain, socket2::Type::DGRAM, Some(socket2::Protocol::UDP))
                .map_err(|e| NetError::Bind(e.to_string()))?;
        socket
            .set_reuse_address(true)
            .map_err(|e| NetError::Bind(e.to_string()))?;
        #[cfg(unix)]
        socket
            .set_reuse_port(true)
            .map_err(|e| NetError::Bind(e.to_string()))?;
        socket
            .set_nonblocking(true)
            .map_err(|e| NetError::Bind(e.to_string()))?;
        socket
            .bind(&addr.into())
            .map_err(|e| NetError::Bind(e.to_string()))?;
        socket
            .join_multicast_v4(&group, &interface)
            .map_err(|e| NetError::JoinMulticast(e.to_string()))?;
        let std_sock: std::net::UdpSocket = socket.into();
        let tokio_sock =
            tokio::net::UdpSocket::from_std(std_sock).map_err(|e| NetError::Bind(e.to_string()))?;
        Ok(Box::new(TokioUdp(tokio_sock)))
    }
}

pub struct TokioTcpConnect;

#[async_trait]
impl TcpConnect for TokioTcpConnect {
    async fn connect(&self, addr: SocketAddr) -> Result<TcpStream, NetError> {
        let stream = tokio::net::TcpStream::connect(addr)
            .await
            .map_err(|e| NetError::Connect(e.to_string()))?;
        Ok(Box::new(stream))
    }
}

// `Mutex` is referenced so future MQTT-backed connection pools (Phase 6+)
// have it in scope without an extra edit.
#[allow(dead_code)]
type _MutexProbe<T> = Mutex<T>;
