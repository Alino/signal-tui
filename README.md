# Signal TUI

A terminal user interface for [signal-cli](https://github.com/AsamK/signal-cli), built with Python and [Textual](https://textual.textualize.io/).

## Features

- Terminal UI for signal-cli
- Support for direct messages and group chats
- Contact search/filtering
- QR code device linking
- Contact caching for fast startup
- Keyboard and mouse navigation

## Prerequisites

### 1. Install signal-cli

Signal TUI requires [signal-cli](https://github.com/AsamK/signal-cli) to be installed.

**macOS (Homebrew):**
```bash
brew install signal-cli
```

**Linux:**
```bash
# Download the latest release from GitHub
wget https://github.com/AsamK/signal-cli/releases/download/v0.13.2/signal-cli-0.13.2.tar.gz
tar xf signal-cli-0.13.2.tar.gz
sudo mv signal-cli-0.13.2 /opt/signal-cli
sudo ln -s /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli
```

Verify installation:
```bash
signal-cli --version
```

### 2. Python 3.10+

**macOS (Homebrew):**
```bash
brew install python@3.11
```

**Or using pyenv:**
```bash
brew install pyenv
pyenv install 3.11
pyenv global 3.11
```

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/signal-tui.git
cd signal-tui
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

Or install with pip:
```bash
pip install -e .
```

## Usage

Run the application:
```bash
python signal_tui.py
```

Or if installed via pip:
```bash
signal-tui
```

### First-time Setup

On first launch, you'll need to link to your account via signal-cli:

1. Open Signal on your phone
2. Go to **Settings > Linked Devices**
3. Tap **+** to add a new device
4. Scan the QR code displayed in Signal TUI

Once linked, your contacts will be loaded and cached for faster startup.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+Q` | Quit |
| `Ctrl+N` | New chat |
| `Ctrl+S` | Focus search box |
| `Ctrl+R` | Refresh contacts |
| `Ctrl+L` | Link new account |
| `Up/Down` | Navigate contacts |
| `Enter` | Select contact / Send message |
| `Escape` | Focus message input |
| `F1` | Show help |

### Mouse

- Click on any contact or group to open the conversation

## Configuration

Configuration is stored in `~/.config/signal-tui/config.json`:

```json
{
  "signal_cli_path": "signal-cli",
  "signal_cli_config_dir": "~/.local/share/signal-cli",
  "phone_number": "+1234567890",
  "theme": "dark",
  "show_timestamps": true,
  "notification_sound": false,
  "use_daemon": true,
  "daemon_port": 7583
}
```

Contact cache is stored in `~/.config/signal-tui/contacts_cache.json` for fast startup.

## Project Structure

```
signal-tui/
├── signal_tui.py      # Main TUI application
├── signal_client.py   # signal-cli wrapper
├── config.py          # Configuration management
├── requirements.txt   # Python dependencies
└── pyproject.toml     # Project metadata
```

## Dependencies

- [textual](https://github.com/Textualize/textual) - TUI framework
- [rich](https://github.com/Textualize/rich) - Terminal formatting
- [qrcode](https://github.com/lincolnloop/python-qrcode) - QR code generation for device linking

## Troubleshooting

### "signal-cli not found"

Make sure signal-cli is installed and in your PATH:
```bash
which signal-cli
```

### "Could not connect"

1. Verify your account is linked: `signal-cli -u +YOUR_NUMBER listContacts`
2. Try re-linking via `Ctrl+L` in the app

### Contacts loading slowly

Contacts are fetched from signal-cli on first load, which can take time with many contacts. Subsequent launches use the cache for instant loading.

## License

MIT License
