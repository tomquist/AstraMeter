//! `SCRIPT` powermeter — run an external command, parse stdout floats.
//! Port of `src/astrameter/powermeter/script.py`. Host-only.

use std::sync::Arc;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::Platform;
use async_trait::async_trait;

pub struct Script {
    command: String,
}

impl Script {
    pub fn new(command: String) -> Self {
        Self { command }
    }
}

#[async_trait]
impl Powermeter for Script {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let output = tokio::process::Command::new("sh")
            .arg("-c")
            .arg(&self.command)
            .output()
            .await
            .map_err(|e| Error::transport(format!("script spawn failed: {e}")))?;
        if !output.status.success() {
            let err = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(Error::transport(format!(
                "Script exited with code {}: {}{}",
                output.status.code().unwrap_or(-1),
                self.command,
                if err.is_empty() {
                    String::new()
                } else {
                    format!("\n{err}")
                }
            )));
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        let mut values = Vec::new();
        for line in stdout.lines() {
            let l = line.trim();
            if l.is_empty() {
                continue;
            }
            let v: f64 = l
                .parse()
                .map_err(|e| Error::decode(format!("script line {l:?}: {e}")))?;
            values.push(v);
        }
        Ok(values)
    }
}

pub fn create(section: &Section<'_>, _platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let command = section.get_required("COMMAND")?.to_string();
    Ok(Arc::new(Script::new(command)))
}
