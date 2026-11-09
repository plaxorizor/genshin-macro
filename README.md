# Genshin Macro for KDE Plasma / Wayland — v0.3

A manually launched, Genshin Impact-only system-tray macro utility for the tested AKKO M1 V5 keyboard and Pulsar Mouse interface.

## What's new in v0.3

- A **Settings…** window is available from the tray menu.
- Enable or disable each macro independently.
- Configure the double-tap interval for number keys 1–5.
- Configure the minimum and maximum repeat intervals for Mouse 4 and Mouse 5.
- Settings are saved between launches with Qt `QSettings`.
- Choose whether macros are enabled automatically when the app is launched.
- Restore all settings to the original defaults.

## Default macros

- Double-tap the same key from `1` to `5` within **200 ms**: send `E`. The physical number-key presses continue normally.
- Hold XButton1 (`BTN_SIDE`): suppress the original side-button event and repeat `F` every **90–110 ms**.
- Hold XButton2 (`BTN_EXTRA`): suppress the original side-button event and repeat left-click every **80–110 ms**.
- Macros act only while the active window title contains `Genshin Impact`.

The default values match the previously tested v0.2 behavior.

## Dependencies

On CachyOS:

```bash
sudo pacman -S python-evdev pyside6
```

Install `kdotool` from the AUR if it is not already installed:

```bash
paru -S kdotool
```

## Permissions and devices

The app expects the following selected physical devices to be accessible by your user, and `/dev/uinput` to be writable:

- `/dev/input/by-id/usb-AKKO_M1_V5-if02-event-kbd`
- `/dev/input/by-id/usb-pulsar_Pulsar_Mouse_IR_update-if02-event-ir`
- `/dev/uinput`

Use the dedicated `genshinmacro` group and the udev rules configured for these devices. Do not add your user to the broad `input` group just for this app.

## Install or update

Extract the archive, open a terminal in the extracted `genshin-macro` directory, and run:

```bash
./install.sh
```

The installer updates the existing application under `~/.local/opt/genshin-macro` and the KDE launcher under `~/.local/share/applications`. It does not create a systemd service or an autostart entry.

Required dependencies can be installed with:

```bash
sudo pacman -S python-evdev pyside6
```

The launcher asks for authentication to load the `uinput` kernel module for the current session if `/dev/uinput` is missing. It does not configure the module to load at boot.

You can also run directly from the extracted directory with:

```bash
./run.sh
```

Launch **Genshin Macro** manually from KDE's Application Launcher whenever you want to use it.

## Tray controls

Right-click the tray icon to:

- Enable or disable all macros for the current run.
- View whether Genshin is detected.
- Open **Settings…**.
- Exit the application.

The runtime Enable/Disable action applies immediately. The Settings window's **Enable macros automatically when the app launches** option controls the initial state on the next launch; it does not turn macros on or off in the current run.

## Settings

The Settings window contains:

- Double-tap enable/disable and interval (10–2000 ms).
- Mouse 4 enable/disable and repeat interval (10–2000 ms).
- Mouse 5 enable/disable and repeat interval (10–2000 ms).
- Startup preference for whether macros are enabled when the app launches.

Settings are saved to the user's Qt configuration file, normally under:

```text
~/.config/GenshinMacro/GenshinMacro.conf
```

Press **Save** to apply and persist changes. **Cancel** discards unsaved changes. **Restore defaults** restores the original timings and enables all three macros.

## Mouse behavior and safety

While this app is running, it grabs the selected physical Pulsar mouse event device and forwards its events through a virtual mouse. The virtual mouse reuses the physical mouse's identity so KDE can apply the same per-device pointer settings. XButton1/XButton2 are consumed only when macros are enabled globally, the corresponding macro is enabled, and Genshin is focused. Exit the app to release the grab and return to normal physical-device handling.

If mouse behavior looks abnormal, exit the app; the physical mouse should resume normal operation.
