fn main() {
    // WinLibs GCC 16 adds default-manifest.o unconditionally. Keep the one
    // application manifest below, including PerMonitorV2/asInvoker, by changing
    // only this executable's endfile spec. Never edit the shared toolchain.
    if std::env::var("TARGET")
        .unwrap_or_default()
        .ends_with("windows-gnu")
    {
        let linker =
            std::env::var("RUSTC_LINKER").unwrap_or_else(|_| "x86_64-w64-mingw32-gcc".into());
        if let Ok(result) = std::process::Command::new(linker)
            .arg("-dumpspecs")
            .output()
        {
            let specs = String::from_utf8_lossy(&result.stdout).replace("\r\n", "\n");
            if let Some(body) = specs
                .split("*endfile:\n")
                .nth(1)
                .and_then(|s| s.split("\n\n").next())
            {
                let default = "%{!shared:%:if-exists(default-manifest.o%s)}";
                if body.contains(default) {
                    let path = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap())
                        .join("app-endfile.specs");
                    std::fs::write(
                        &path,
                        format!("*endfile:\n{}\n\n", body.replace(default, "")),
                    )
                    .unwrap();
                    println!("cargo:rustc-link-arg=-specs={}", path.display());
                }
            }
        }
    }
    tauri_build::try_build(tauri_build::Attributes::new().windows_attributes(
        tauri_build::WindowsAttributes::new().app_manifest(include_str!("app.manifest")),
    ))
    .expect("desktop resource build failed")
}
