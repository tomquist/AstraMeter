// Build script. On esp-idf targets, embuild applies sdkconfig.defaults
// and partitions.csv; on other targets it's a no-op so the workspace
// still builds for hosts.

fn main() {
    #[cfg(target_os = "espidf")]
    {
        embuild::espidf::sysenv::output();
    }
}
