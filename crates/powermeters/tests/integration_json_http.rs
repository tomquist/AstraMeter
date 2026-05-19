//! End-to-end integration test: real reqwest -> wiremock HTTP server,
//! through the platform layer and the `json_http` powermeter.

use std::sync::Arc;

use astrameter_config::Config;
use astrameter_platform_std::build_platform;
use astrameter_powermeters::{register_all, PowermeterRegistry};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn json_http_reads_value_via_real_http_server() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/meter"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "power": {"total": 4321.5},
        })))
        .mount(&server)
        .await;

    let cfg_text = format!(
        "[JSON_HTTP]\nURL = {url}/meter\nJSON_PATHS = $.power.total\n",
        url = server.uri()
    );
    let cfg = Config::parse(&cfg_text).expect("parse");
    let section = cfg.section("JSON_HTTP").expect("section");

    let platform = Arc::new(build_platform());
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);
    let factory = reg.lookup("JSON_HTTP").expect("factory");
    let meter = factory(&section, platform).expect("create");

    meter.start().await.expect("start");
    let values = meter.get_powermeter_watts().await.expect("read");
    assert_eq!(values, vec![4321.5]);
}
