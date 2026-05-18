use astrameter_platform::mqtt::{MqttError, MqttFactory, MqttOptions};

pub struct RumqttcFactory;

impl MqttFactory for RumqttcFactory {
    fn connect(
        &self,
        opts: MqttOptions,
    ) -> Result<(rumqttc::AsyncClient, rumqttc::EventLoop), MqttError> {
        let mut mqttoptions = rumqttc::MqttOptions::new(opts.client_id, opts.host, opts.port);
        mqttoptions.set_keep_alive(opts.keep_alive);
        mqttoptions.set_clean_session(opts.clean_session);
        if let (Some(u), Some(p)) = (opts.username.as_ref(), opts.password.as_ref()) {
            mqttoptions.set_credentials(u, p);
        }
        if opts.tls {
            mqttoptions
                .set_transport(rumqttc::Transport::Tls(rumqttc::TlsConfiguration::default()));
        }
        let (client, eventloop) = rumqttc::AsyncClient::new(mqttoptions, 32);
        let _ = MqttError::Closed;
        Ok((client, eventloop))
    }
}
