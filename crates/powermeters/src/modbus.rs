//! `MODBUS` — Modbus TCP register read. Port of
//! `src/astrameter/powermeter/modbus.py`.

use std::net::SocketAddr;
use std::sync::Arc;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::net::TcpConnect;
use astrameter_platform::Platform;
use async_trait::async_trait;
use tokio::sync::Mutex;
use tokio_modbus::client::{tcp, Context, Reader};
use tokio_modbus::prelude::Client;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DataType {
    Float32,
    Int16,
    Uint16,
    Int32,
    Uint32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Endianness {
    Big,
    Little,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RegisterType {
    Holding,
    Input,
}

pub struct ModbusPowermeter {
    host: String,
    port: u16,
    unit_id: u8,
    address: u16,
    count: u16,
    data_type: DataType,
    byte_order: Endianness,
    word_order: Endianness,
    register_type: RegisterType,
    ctx: Mutex<Option<Context>>,
    /// Plumb every TCP connect through the platform so the ESP32 build
    /// gets its blocking-`std::net` wrapper while the host keeps using
    /// `tokio::net::TcpStream` — `tokio_modbus::client::tcp::attach_slave`
    /// works with any `AsyncRead + AsyncWrite`.
    tcp: Arc<dyn TcpConnect>,
}

fn decode(
    regs: &[u16],
    data_type: DataType,
    byte_order: Endianness,
    word_order: Endianness,
) -> Result<f64> {
    // Convert each u16 to two bytes according to byte_order, then concatenate
    // following word_order.
    fn u16_to_bytes(r: u16, byte: Endianness) -> [u8; 2] {
        match byte {
            Endianness::Big => r.to_be_bytes(),
            Endianness::Little => r.to_le_bytes(),
        }
    }
    let mut bytes = Vec::with_capacity(regs.len() * 2);
    let mut words = regs.to_vec();
    if word_order == Endianness::Little {
        words.reverse();
    }
    for w in words {
        bytes.extend_from_slice(&u16_to_bytes(w, byte_order));
    }
    Ok(match data_type {
        DataType::Int16 => i16::from_be_bytes(bytes[..2].try_into().unwrap()) as f64,
        DataType::Uint16 => u16::from_be_bytes(bytes[..2].try_into().unwrap()) as f64,
        DataType::Int32 => i32::from_be_bytes(bytes[..4].try_into().unwrap()) as f64,
        DataType::Uint32 => u32::from_be_bytes(bytes[..4].try_into().unwrap()) as f64,
        DataType::Float32 => f32::from_be_bytes(bytes[..4].try_into().unwrap()) as f64,
    })
}

#[async_trait]
impl Powermeter for ModbusPowermeter {
    async fn start(&self) -> Result<()> {
        // Resolve the host the way Python pymodbus does (hostname or IP).
        let addr_str = format!("{}:{}", self.host, self.port);
        let socket: SocketAddr = if let Ok(s) = addr_str.parse() {
            s
        } else {
            tokio::net::lookup_host(&addr_str)
                .await
                .map_err(|e| Error::transport(format!("modbus DNS lookup {addr_str}: {e}")))?
                .next()
                .ok_or_else(|| Error::config(format!("modbus: no addresses for {addr_str}")))?
        };
        let stream = self
            .tcp
            .connect(socket)
            .await
            .map_err(|e| Error::transport(format!("modbus connect: {e}")))?;
        let ctx = tcp::attach_slave(stream, tokio_modbus::Slave(self.unit_id));
        *self.ctx.lock().await = Some(ctx);
        Ok(())
    }

    async fn stop(&self) -> Result<()> {
        let mut guard = self.ctx.lock().await;
        if let Some(mut ctx) = guard.take() {
            let _ = ctx.disconnect().await;
        }
        Ok(())
    }

    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let mut guard = self.ctx.lock().await;
        if guard.is_none() {
            drop(guard);
            self.start().await?;
            guard = self.ctx.lock().await;
        }
        let ctx = guard
            .as_mut()
            .ok_or_else(|| Error::transport("modbus: no context"))?;
        let regs = match self.register_type {
            RegisterType::Holding => ctx.read_holding_registers(self.address, self.count).await,
            RegisterType::Input => ctx.read_input_registers(self.address, self.count).await,
        };
        let regs = regs
            .map_err(|e| Error::transport(format!("modbus read: {e}")))?
            .map_err(|e| Error::transport(format!("modbus exception: {e}")))?;
        let v = decode(&regs, self.data_type, self.byte_order, self.word_order)?;
        Ok(vec![v])
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let data_type = match section
        .get_str("DATA_TYPE", "UINT16")
        .to_uppercase()
        .as_str()
    {
        "FLOAT32" => DataType::Float32,
        "INT16" => DataType::Int16,
        "UINT16" => DataType::Uint16,
        "INT32" => DataType::Int32,
        "UINT32" => DataType::Uint32,
        other => return Err(Error::config(format!("modbus DATA_TYPE {other:?}"))),
    };
    let byte_order = match section.get_str("BYTE_ORDER", "BIG").to_uppercase().as_str() {
        "BIG" => Endianness::Big,
        "LITTLE" => Endianness::Little,
        other => return Err(Error::config(format!("modbus BYTE_ORDER {other:?}"))),
    };
    let word_order = match section.get_str("WORD_ORDER", "BIG").to_uppercase().as_str() {
        "BIG" => Endianness::Big,
        "LITTLE" => Endianness::Little,
        other => return Err(Error::config(format!("modbus WORD_ORDER {other:?}"))),
    };
    let register_type = match section
        .get_str("REGISTER_TYPE", "HOLDING")
        .to_uppercase()
        .as_str()
    {
        "HOLDING" => RegisterType::Holding,
        "INPUT" => RegisterType::Input,
        other => return Err(Error::config(format!("modbus REGISTER_TYPE {other:?}"))),
    };
    Ok(Arc::new(ModbusPowermeter {
        host: section.get_required("HOST")?.to_string(),
        port: section.get_int("PORT", 502)? as u16,
        unit_id: section.get_int("UNIT_ID", 1)? as u8,
        address: section.get_int("ADDRESS", 0)? as u16,
        count: section.get_int("COUNT", 1)? as u16,
        data_type,
        byte_order,
        word_order,
        register_type,
        ctx: Mutex::new(None),
        tcp: platform.tcp.clone(),
    }))
}
