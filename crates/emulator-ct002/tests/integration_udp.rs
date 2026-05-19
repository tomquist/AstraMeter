//! End-to-end integration test: bind the CT002 emulator on a random UDP
//! port, send a request matching the real CT002 wire format, verify the
//! parsed response.

use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;

use astrameter_config::ClientFilter;
use astrameter_core::{Powermeter, Result};
use astrameter_emulator_ct002::balancer::BalancerConfig;
use astrameter_emulator_ct002::protocol::{build_payload, parse_request};
use astrameter_emulator_ct002::server::{BoundMeter, Ct002Emulator};
#[allow(unused_imports)]
use astrameter_platform::net::UdpSocket as _; // bring the trait into scope for `client.send_to(...)`
use astrameter_platform_std::build_platform;
use async_trait::async_trait;

struct Fixed(Vec<f64>);

#[async_trait]
impl Powermeter for Fixed {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        Ok(self.0.clone())
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ct002_request_response_round_trip() {
    let platform = Arc::new(build_platform());
    let probe = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    let port = probe.local_addr().unwrap().port();
    drop(probe);

    let meter: Arc<dyn Powermeter> = Arc::new(Fixed(vec![123.0, 0.0, 0.0]));
    let bound = BoundMeter {
        meter,
        filter: ClientFilter::allow_all(),
        wait_for_next: false,
    };
    let emu = Ct002Emulator::new(
        port,
        "AABBCCDDEEFF".into(),
        vec![bound],
        BalancerConfig::default(),
        platform.clone(),
    );
    emu.start().await.expect("start");
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;

    let client = platform
        .udp
        .bind("127.0.0.1:0".parse().unwrap())
        .await
        .expect("client bind");
    let target: SocketAddr = (Ipv4Addr::LOCALHOST, port).into();
    // Real CT002 requests carry meter_dev_type / meter_mac_code / ct_type
    // / ct_mac / phase / power. Use inspection-mode phase ("0") so the
    // balancer is bypassed and the response forwards the raw grid reading.
    let frame = build_payload(&["HMG-50", "112233445566", "HME-4", "AABBCCDDEEFF", "0", "0"])
        .expect("build");
    client.send_to(&frame, target).await.expect("send");

    let mut buf = vec![0u8; 4096];
    let (n, _) = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        client.recv_from(&mut buf),
    )
    .await
    .expect("timeout")
    .expect("recv");
    let fields = parse_request(&buf[..n]).expect("parse");
    // RESPONSE_LABELS is 24 entries.
    assert_eq!(fields.len(), 24);
    // Field 4 ("A_phase_power") should be "123".
    assert_eq!(fields[4], "123");
    assert_eq!(fields[1], "AABBCCDDEEFF"); // meter_mac_code echoed
    emu.stop().await;
}
