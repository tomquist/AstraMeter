use thiserror::Error;

pub type Result<T, E = Error> = core::result::Result<T, E>;

#[derive(Debug, Error)]
pub enum Error {
    #[error("configuration error: {0}")]
    Config(String),

    #[error("transport error: {0}")]
    Transport(String),

    #[error("decode error: {0}")]
    Decode(String),

    #[error("no value yet")]
    NoValue,

    #[error("stale measurement: {age_secs:.1}s old (max {max_secs:.1}s)")]
    Stale { age_secs: f64, max_secs: f64 },

    #[error("unsupported on this platform: {0}")]
    UnsupportedOnPlatform(&'static str),

    #[error("timed out after {millis}ms")]
    Timeout { millis: u64 },

    #[error("{0}")]
    Other(String),
}

impl Error {
    pub fn config(msg: impl Into<String>) -> Self {
        Error::Config(msg.into())
    }
    pub fn transport(msg: impl Into<String>) -> Self {
        Error::Transport(msg.into())
    }
    pub fn decode(msg: impl Into<String>) -> Self {
        Error::Decode(msg.into())
    }
}
