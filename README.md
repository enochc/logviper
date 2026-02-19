# LogViper 🐍📋

**Cross-platform multi-file synchronized log viewer** built with Python + Textual TUI.

## Features

- **4-panel simultaneous view** — up to 4 log files side by side
- **Timestamp synchronization** — sync scroll position across all panels by timestamp
- **Global search & highlight** — regex search highlights matches across ALL open files
- **Recursive file browser** — search any directory recursively for log files
- **Log rollover support** — auto-loads `.log.1`, `.log.2` etc. chains in chronological order
- **Live file watching** — panels auto-update when log files change
- **Smart colorization** — ERROR/WARN/INFO/DEBUG levels auto-colored
- **Cross-platform** — works on macOS, Linux, Windows (any terminal)

## Install

```bash
# Quick (local)
pip3 install textual watchdog
python3 logviper.py

# Or run the installer
chmod +x install.sh && ./install.sh

# Global install (adds `logviper` command)
./install.sh --global
```

## Usage

```bash
# Open with files
python3 logviper.py /var/log/syslog /var/log/auth.log

# Open file browser on launch
python3 logviper.py

# Multiple files
python3 logviper.py app.log worker.log nginx.log db.log
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `O` | Open file browser (for focused panel) |
| `1`-`4` | Focus panel 1-4 |
| `/` | Focus search bar |
| `Enter` | Execute search across all panels |
| `F3` / `N` | Next match |
| `Shift+F3` | Previous match |
| `Esc` | Clear highlights |
| `S` | Sync all panels to focused panel's timestamp |
| `F` | Toggle follow/tail mode |
| `?` | Help screen |
| `Q` | Quit |

## Search Examples

```
error|warn|fail      # Multiple keywords
\[ERROR\]            # Regex patterns  
timeout.*connection  # Complex patterns
192\.168\.\d+        # IP addresses
```

## Log Rollover

When you open `app.log`, LogViper automatically finds and loads:
```
app.log.5  (oldest)
app.log.4
app.log.3
app.log.2
app.log.1
app.log    (newest)
```
All loaded chronologically as a single continuous stream.

## Timestamp Sync

LogViper detects timestamps in common formats:
- ISO 8601: `2024-01-15T10:30:45.123`
- Syslog: `Jan 15 10:30:45`  
- Android: `01-15 10:30:45.123`
- Apache: `15/Jan/2024:10:30:45`
- Epoch (ms/s): `1705316345123`
- Time only: `10:30:45.123`

Press `S` to synchronize all panels to the timestamp at the top of the focused panel.

## Requirements

- Python 3.8+
- `textual >= 0.47`
- `watchdog >= 3.0`
