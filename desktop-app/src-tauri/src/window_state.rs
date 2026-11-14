use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use tauri::{LogicalPosition, LogicalSize, Window};

static SAVE_REVISION: AtomicU64 = AtomicU64::new(0);
static FIT_REVISION: AtomicU64 = AtomicU64::new(0);
static INITIALIZED: AtomicBool = AtomicBool::new(false);
static FITTING: AtomicBool = AtomicBool::new(false);
static PLACEMENT_REVISION: AtomicU64 = AtomicU64::new(0);

pub fn initialize(window: &Window) {
    // Window creation emits move/resize events before setup runs. Those events
    // must not overwrite the persisted placement before it has been restored.
    fit(window, true);
}

// Native sizing commands are queued. Save after Windows finishes the whole
// maximize/restore transition, rather than recording an intermediate rectangle.
pub fn schedule_save(window: &Window) {
    if !INITIALIZED.load(Ordering::Relaxed) || FITTING.load(Ordering::Relaxed) {
        return;
    }
    let revision = SAVE_REVISION.fetch_add(1, Ordering::Relaxed) + 1;
    let window = window.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(220)).await;
        if SAVE_REVISION.load(Ordering::Relaxed) != revision {
            return;
        }
        let target = window.clone();
        let _ = window.run_on_main_thread(move || {
            if SAVE_REVISION.load(Ordering::Relaxed) == revision {
                save(&target);
            }
        });
    });
}

pub fn schedule_fit(window: &Window) {
    if !INITIALIZED.load(Ordering::Relaxed) {
        return;
    }
    let revision = FIT_REVISION.fetch_add(1, Ordering::Relaxed) + 1;
    let window = window.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(220)).await;
        if FIT_REVISION.load(Ordering::Relaxed) != revision {
            return;
        }
        let target = window.clone();
        let _ = window.run_on_main_thread(move || {
            if FIT_REVISION.load(Ordering::Relaxed) == revision {
                fit(&target, true);
                schedule_save(&target);
            }
        });
    });
}

fn path() -> Option<PathBuf> {
    let base = crate::profile::data()?;
    Some(base.join("state/window-bounds.json"))
}
pub fn save(window: &Window) {
    if !INITIALIZED.load(Ordering::Relaxed) || FITTING.load(Ordering::Relaxed) {
        return;
    }
    if window.is_minimized().unwrap_or(true) {
        return;
    }
    let maximized = window.is_maximized().unwrap_or(false);
    let (Ok(pos), Ok(size), Ok(scale), Some(path)) = (
        window.outer_position(),
        window.inner_size(),
        window.scale_factor(),
        path(),
    ) else {
        return;
    };
    let previous = std::fs::read(&path)
        .ok()
        .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
    let mut value = if maximized {
        previous.unwrap_or(serde_json::json!({"width":1280.,"height":820.}))
    } else {
        serde_json::json!({"x":pos.x,"y":pos.y,
        "width":f64::from(size.width)/scale,"height":f64::from(size.height)/scale})
    };
    value["maximized"] = maximized.into();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let temporary = path.with_extension("tmp");
    if std::fs::write(&temporary, value.to_string()).is_ok() {
        let _ = std::fs::rename(temporary, path);
    }
}
pub fn fit(window: &Window, restore: bool) {
    let saved = if restore {
        path()
            .and_then(|p| std::fs::read(p).ok())
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
    } else {
        None
    };
    let monitors = window.available_monitors().unwrap_or_default();
    let monitor = saved
        .as_ref()
        .and_then(|v| {
            let x = v["x"].as_f64()?;
            let y = v["y"].as_f64()?;
            monitors
                .iter()
                .find(|m| {
                    let a = m.work_area();
                    x >= f64::from(a.position.x)
                        && y >= f64::from(a.position.y)
                        && x < f64::from(a.position.x) + f64::from(a.size.width)
                        && y < f64::from(a.position.y) + f64::from(a.size.height)
                })
                .cloned()
        })
        .or_else(|| window.current_monitor().ok().flatten())
        .or_else(|| window.primary_monitor().ok().flatten());
    let Some(monitor) = monitor else {
        return;
    };
    FITTING.store(true, Ordering::Relaxed);
    let revision = PLACEMENT_REVISION.fetch_add(1, Ordering::Relaxed) + 1;
    let scale = monitor.scale_factor();
    let area = monitor.work_area();
    let max_w = (f64::from(area.size.width) / scale - 24.).max(320.);
    let max_h = (f64::from(area.size.height) / scale - 48.).max(240.);
    let read = |key: &str, default: f64| {
        saved
            .as_ref()
            .and_then(|s| s[key].as_f64())
            .filter(|n| n.is_finite())
            .unwrap_or(default)
    };
    let width = read("width", 1280.).clamp(320., max_w);
    let height = read("height", 820.).clamp(240., max_h);
    let left = f64::from(area.position.x) / scale;
    let top = f64::from(area.position.y) / scale;
    let x = (read("x", (left + (max_w - width) / 2.) * scale) / scale)
        .clamp(left, left + max_w - width);
    let y =
        (read("y", (top + (max_h - height) / 2.) * scale) / scale).clamp(top, top + max_h - height);
    let _ = window.unmaximize();
    let _ = window.set_min_size(Some(LogicalSize::new(
        720_f64.min(max_w),
        440_f64.min(max_h),
    )));
    let _ = window.set_size(LogicalSize::new(width, height));
    let _ = window.set_position(LogicalPosition::new(x, y));
    let maximized = saved.as_ref().is_some_and(|v| v["maximized"] == true);
    let window = window.clone();
    // Tao's Windows set_outer_position asynchronously clears MAXIMIZED. Apply
    // the saved maximization after that move completes, then allow persistence.
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(220)).await;
        let target = window.clone();
        let _ = window.run_on_main_thread(move || {
            if PLACEMENT_REVISION.load(Ordering::Relaxed) == revision && maximized {
                let _ = target.maximize();
            }
        });
        tokio::time::sleep(std::time::Duration::from_millis(220)).await;
        let target = window.clone();
        let _ = window.run_on_main_thread(move || {
            if PLACEMENT_REVISION.load(Ordering::Relaxed) == revision {
                FITTING.store(false, Ordering::Relaxed);
                INITIALIZED.store(true, Ordering::Relaxed);
                schedule_save(&target);
            }
        });
    });
}
