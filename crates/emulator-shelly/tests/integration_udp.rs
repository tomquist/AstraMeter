//! End-to-end integration test for the Shelly emulator: bind on a random
//! UDP port, send an EM.GetStatus RPC from a fake client, verify the
//! response carries the upstream powermeter's values.

use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;

use astrameter_config::ClientFilter;
use astrameter_core::{Powermeter, Result};
use astrameter_emulator_shelly::{BoundMeter, ShellyEmulator};
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
async fn shelly_em_get_status_round_trip() {
    let platform = Arc::new(build_platform());
    // Bind the emulator on an OS-assigned port.
    let sock = platform
        .udp
        .bind("127.0.0.1:0".parse().unwrap())
        .await
        .expect("bind picker");
    drop(sock); // free the port; emulator will rebind.

    // Instead, ask the OS to pick a port by binding 0 and reading it back.
    let probe = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    let port = probe.local_addr().unwrap().port();
    drop(probe);

    let meter: Arc<dyn Powermeter> = Arc::new(Fixed(vec![100.0, 200.0, 300.0]));
    let bound = BoundMeter {
        meter,
        filter: ClientFilter::allow_all(),
        wait_for_next: false,
    };
    let emu = ShellyEmulator::new(
        port,
        "test-device".into(),
        vec![bound],
        std::time::Duration::ZERO,
        platform.clone(),
    );
    emu.start().await.expect("start");

    // Small grace for the listener to bind.
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;

    let client = platform
        .udp
        .bind("127.0.0.1:0".parse().unwrap())
        .await
        .expect("client bind");
    let request = br#"{"id":1,"method":"EM.GetStatus","params":{"id":0}}"#;
    let target: SocketAddr = (Ipv4Addr::LOCALHOST, port).into();
    client.send_to(request, target).await.expect("send");
    let mut buf = vec![0u8; 4096];
    let (n, _) = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        client.recv_from(&mut buf),
    )
    .await
    .expect("recv timeout")
    .expect("recv ok");
    let body: serde_json::Value = serde_json::from_slice(&buf[..n]).expect("json");
    let total = body
        .pointer("/result/total_act_power")
        .and_then(|v| v.as_f64())
        .expect("total_act_power");
    // 100+200+300 = 600; emulator adds a 0.001 "decimal point enforcer" nudge
    // when the value is exactly integer-valued.
    assert!((total - 600.001).abs() < 1e-3, "got {total}");
    emu.stop().await;
}
