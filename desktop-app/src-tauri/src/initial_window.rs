//! The native window is created hidden; no startup path briefly shows then hides it.
use std::sync::atomic::{AtomicBool, Ordering};

pub struct InitialWindow(AtomicBool);

impl InitialWindow {
    pub const fn new() -> Self {
        Self(AtomicBool::new(true))
    }

    pub fn user_opened(&self) {
        self.0.store(false, Ordering::SeqCst);
    }

    pub fn snapshot(&self, configured: bool, migration: bool, login: bool, tray: bool) -> bool {
        self.0.swap(false, Ordering::SeqCst) && !login && (!configured || migration || !tray)
    }

    pub fn failed(&self, login: bool) -> bool {
        !login && self.0.swap(false, Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn background_launch_never_needs_a_hide_transition() {
        for (configured, migration, login, tray) in [
            (true, false, true, false),
            (true, false, true, true),
            (false, false, true, false),
            (false, true, true, true),
            (true, false, false, true),
        ] {
            let window = InitialWindow::new();
            assert!(!window.snapshot(configured, migration, login, tray));
        }
        assert!(InitialWindow::new().snapshot(true, false, false, false));
    }

    #[test]
    fn first_use_and_migration_are_visible_on_manual_launch() {
        assert!(InitialWindow::new().snapshot(false, false, false, false));
        assert!(InitialWindow::new().snapshot(true, true, false, true));
    }

    #[test]
    fn tray_open_and_close_are_not_overruled_by_a_late_snapshot() {
        let window = InitialWindow::new();
        window.user_opened();
        assert!(!window.snapshot(true, false, true, true));
        assert!(!window.failed(false));
    }

    #[test]
    fn service_failure_keeps_login_hidden_but_manual_launch_recoverable() {
        let window = InitialWindow::new();
        assert!(!window.failed(true));
        assert!(!window.snapshot(false, false, true, true));
        let manual = InitialWindow::new();
        assert!(manual.failed(false));
        assert!(!manual.snapshot(true, false, false, false));
    }
}
