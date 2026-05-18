use astrameter_core::{Error, Result};
use url::Url;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MqttUriParts {
    pub host: String,
    pub port: u16,
    pub username: Option<String>,
    pub password: Option<String>,
    pub tls: bool,
}

/// Parse an `mqtt://` / `mqtts://` URI into its connection parts. Mirrors
/// Python `parse_mqtt_uri`.
pub fn parse_mqtt_uri(uri: &str) -> Result<MqttUriParts> {
    let raw = uri.trim();
    if raw.is_empty() {
        return Err(Error::config("MQTT URI is empty"));
    }
    let parsed =
        Url::parse(raw).map_err(|e| Error::config(format!("MQTT URI parse error: {e}")))?;
    let scheme = parsed.scheme().to_ascii_lowercase();
    if scheme != "mqtt" && scheme != "mqtts" {
        return Err(Error::config(format!(
            "Unsupported MQTT URI scheme '{}'; expected 'mqtt' or 'mqtts'",
            parsed.scheme()
        )));
    }
    let host = parsed
        .host_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| Error::config(format!("MQTT URI is missing a host: {uri:?}")))?
        .to_string();
    if parsed.path() != "" && parsed.path() != "/" {
        return Err(Error::config(format!(
            "MQTT URI must not contain a path: {uri:?}"
        )));
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return Err(Error::config(format!(
            "MQTT URI must not contain query or fragment: {uri:?}"
        )));
    }
    let tls = scheme == "mqtts";
    let port = parsed.port().unwrap_or(if tls { 8883 } else { 1883 });
    let username = match parsed.username() {
        "" => None,
        u => {
            let decoded = percent_decode(u)?;
            Some(decoded)
        }
    };
    let password = match parsed.password() {
        None => None,
        Some(p) => Some(percent_decode(p)?),
    };
    Ok(MqttUriParts {
        host,
        port,
        username,
        password,
        tls,
    })
}

fn percent_decode(s: &str) -> Result<String> {
    let decoded: Result<String> = url::form_urlencoded::parse(s.as_bytes())
        .map(|(k, _)| Ok(k.into_owned()))
        .collect();
    // The above turns "a:b" into "a:b" but loses the form structure; use a
    // simpler manual decode instead.
    let _ = decoded; // suppress unused
    let mut out = String::with_capacity(s.len());
    let mut bytes = s.as_bytes().iter().copied();
    while let Some(b) = bytes.next() {
        if b == b'%' {
            let hi = bytes
                .next()
                .ok_or_else(|| Error::config(format!("bad percent escape in {s:?}")))?;
            let lo = bytes
                .next()
                .ok_or_else(|| Error::config(format!("bad percent escape in {s:?}")))?;
            let val = hex_digit(hi)? * 16 + hex_digit(lo)?;
            out.push(val as char);
        } else {
            out.push(b as char);
        }
    }
    Ok(out)
}

fn hex_digit(b: u8) -> Result<u8> {
    match b {
        b'0'..=b'9' => Ok(b - b'0'),
        b'a'..=b'f' => Ok(b - b'a' + 10),
        b'A'..=b'F' => Ok(b - b'A' + 10),
        _ => Err(Error::config(format!("bad hex digit {b:#x}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_uri() {
        let p = parse_mqtt_uri("mqtt://broker.example:1884").unwrap();
        assert_eq!(p.host, "broker.example");
        assert_eq!(p.port, 1884);
        assert!(!p.tls);
        assert!(p.username.is_none());
    }

    #[test]
    fn mqtts_default_port() {
        let p = parse_mqtt_uri("mqtts://b.example").unwrap();
        assert_eq!(p.port, 8883);
        assert!(p.tls);
    }

    #[test]
    fn user_pass_decoded() {
        let p = parse_mqtt_uri("mqtt://us%40er:p%3Aass@host").unwrap();
        assert_eq!(p.username.as_deref(), Some("us@er"));
        assert_eq!(p.password.as_deref(), Some("p:ass"));
    }

    #[test]
    fn rejects_bad_scheme() {
        assert!(parse_mqtt_uri("http://host").is_err());
    }
}
