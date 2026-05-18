use astrameter_core::{Error, Result};
use ipnet::Ipv4Net;
use std::net::Ipv4Addr;
use std::str::FromStr;

/// Per-section CIDR filter. Mirrors the Python `ClientFilter` in
/// `config_loader.py`.
#[derive(Debug, Clone)]
pub struct ClientFilter {
    nets: Vec<Ipv4Net>,
}

impl ClientFilter {
    pub fn from_csv(value: &str) -> Result<Self> {
        let mut nets = Vec::new();
        for part in value.split(',') {
            let p = part.trim();
            if p.is_empty() {
                continue;
            }
            let net = Ipv4Net::from_str(p)
                .map_err(|e| Error::config(format!("invalid netmask {p:?}: {e}")))?;
            nets.push(net);
        }
        Ok(Self { nets })
    }

    pub fn allow_all() -> Self {
        Self {
            nets: vec![Ipv4Net::from_str("0.0.0.0/0").unwrap()],
        }
    }

    pub fn matches(&self, addr: Ipv4Addr) -> bool {
        self.nets.iter().any(|n| n.contains(&addr))
    }
}
