// Build script. On esp-idf targets, embuild applies sdkconfig.defaults
// and partitions.csv and wires up the ldproxy linker arguments; on
// other targets it's a no-op so the workspace still builds for hosts.
//
// NOTE: `#[cfg(target_os = "espidf")]` inside build.rs checks the
// *host* OS the script runs on, which is never espidf. We have to
// look at `CARGO_CFG_TARGET_OS` instead to learn what the actual
// build *target* is. Without that, embuild's output never runs and
// ldproxy fails with "Cannot locate argument '--ldproxy-linker'".

fn main() {
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "espidf" {
        embuild::espidf::sysenv::output();
    }
}
