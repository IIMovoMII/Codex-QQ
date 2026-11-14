//! Explicit isolated acceptance profile. Never changes the user's Codex home.
use std::path::PathBuf;
pub fn test_root() -> Option<PathBuf> {
    let mut args = std::env::args_os();
    while let Some(arg) = args.next() {
        if arg == "--test-profile" {
            let root = args
                .next()
                .map(PathBuf::from)
                .filter(|p| p.is_absolute() && p.parent().is_some());
            if root.is_none() {
                std::process::exit(4);
            }
            return root;
        }
    }
    None
}
pub fn data() -> Option<PathBuf> {
    test_root()
        .map(|p| p.join("data"))
        .or_else(|| std::env::var_os("CODEX_QQ_DATA_DIR").map(PathBuf::from))
        .or_else(|| {
            std::env::var_os("LOCALAPPDATA").map(|p| PathBuf::from(p).join("CodexDesktopWeixin"))
        })
}
