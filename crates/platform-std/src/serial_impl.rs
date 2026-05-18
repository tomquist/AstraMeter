use astrameter_platform::serial::{Parity, SerialConfig, SerialError, SerialPort, SerialStream};
use async_trait::async_trait;
use tokio::io::AsyncReadExt;

pub struct TokioSerial;

struct TokioSerialStream(tokio_serial::SerialStream);

#[async_trait]
impl SerialStream for TokioSerialStream {
    async fn read(&mut self, buf: &mut [u8]) -> Result<usize, SerialError> {
        self.0
            .read(buf)
            .await
            .map_err(|e| SerialError::Read(e.to_string()))
    }
}

#[async_trait]
impl SerialPort for TokioSerial {
    async fn open(&self, cfg: SerialConfig) -> Result<Box<dyn SerialStream>, SerialError> {
        use tokio_serial::SerialPortBuilderExt;
        let stop = match cfg.stop_bits {
            1 => tokio_serial::StopBits::One,
            2 => tokio_serial::StopBits::Two,
            n => {
                return Err(SerialError::Open(format!("unsupported stop bits: {n}")));
            }
        };
        let parity = match cfg.parity {
            Parity::None => tokio_serial::Parity::None,
            Parity::Odd => tokio_serial::Parity::Odd,
            Parity::Even => tokio_serial::Parity::Even,
        };
        let data_bits = match cfg.data_bits {
            5 => tokio_serial::DataBits::Five,
            6 => tokio_serial::DataBits::Six,
            7 => tokio_serial::DataBits::Seven,
            8 => tokio_serial::DataBits::Eight,
            n => return Err(SerialError::Open(format!("unsupported data bits: {n}"))),
        };
        let stream = tokio_serial::new(&cfg.path, cfg.baud_rate)
            .data_bits(data_bits)
            .stop_bits(stop)
            .parity(parity)
            .open_native_async()
            .map_err(|e| SerialError::Open(e.to_string()))?;
        Ok(Box::new(TokioSerialStream(stream)))
    }
}
