use sha2::{Digest, Sha256};
use std::path::Path;
pub fn verify(root: &Path) -> Result<(), String> {
    let raw =
        std::fs::read(root.join("build-manifest.json")).map_err(|_| "安装清单缺失，请修复安装")?;
    let manifest: serde_json::Value =
        serde_json::from_slice(&raw).map_err(|_| "安装清单无效，请修复安装")?;
    if manifest["version"] != env!("CARGO_PKG_VERSION") {
        return Err("运行组件版本不匹配，请修复安装".into());
    }
    let files = manifest["files"].as_object().ok_or("安装清单无效")?;
    for (name, expected) in files {
        let relative = Path::new(name);
        if relative.is_absolute()
            || relative
                .components()
                .any(|c| !matches!(c, std::path::Component::Normal(_)))
        {
            return Err("安装资源路径无效".into());
        }
        let file = root.join(relative);
        if !file
            .canonicalize()
            .map_err(|_| "安装组件缺失")?
            .starts_with(root.canonicalize().map_err(|_| "安装目录无效")?)
        {
            return Err("安装资源路径无效".into());
        }
        let bytes = std::fs::read(file).map_err(|_| "安装组件不可读")?;
        if expected.as_str() != Some(format!("{:x}", Sha256::digest(&bytes)).as_str()) {
            return Err("安装组件校验失败，请修复安装".into());
        }
    }
    Ok(())
}
