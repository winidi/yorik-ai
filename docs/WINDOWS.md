# Yorik on Windows

Yorik runs on Windows as the same Docker stack as everywhere else, in
Docker Desktop on WSL2. The setup does all of that for you.

**The caveat first:** a PC that is off at night is not a home server.
Calendar reminders, the WhatsApp bridge and email fetching want to run
around the clock. Windows is the right way to *try* Yorik on the PC you
already have. For the family, a used mini PC with the install stick
([INSTALL.md](INSTALL.md#a-mini-pc-as-the-familys-box)) is the better
home, and the Windows PC then uses Yorik in the browser.

## Install

1. From the [latest release](https://github.com/winidi/yorik-ai/releases/latest)
   download `Yorik-Setup-Windows.zip` and unzip it.
2. Double-click `Yorik-Setup.cmd`, allow administrator rights.

Windows 10 22H2 or Windows 11. What it does:

- turns on WSL2 and installs Docker Desktop with winget, if missing.
  Windows then restarts **once**; after you sign in again the setup
  continues by itself (Docker Desktop may ask you to accept its terms —
  it's free for personal use and small businesses);
- sets Docker Desktop to start with Windows;
- puts Yorik in `%LOCALAPPDATA%\Yorik`, backups in `Documents\Yorik Backups`;
- starts Yorik, puts a "Yorik" shortcut on the desktop and opens the
  browser, where you create your account.

The first start downloads several GB and can take up to 20 minutes.

## After the install

**Phones at home** reach Yorik at `http://<this PC's address>:8000`. If
Windows asks whether Docker may accept connections, allow it for private
networks. **On the go**: Yorik → Settings → System → Phones, sign in to
Tailscale once.

**Memory.** WSL2 takes half the RAM by default. With 16 GB that's tight
for Yorik, photos and documents together. Create
`C:\Users\<you>\.wslconfig`:

```
[wsl2]
memory=12GB
swap=4GB
```

then in PowerShell `wsl --shutdown` and start Docker Desktop again.

**Keep it running.** Yorik runs while Docker Desktop runs. Switch off
sleep while you rely on it (Settings → System → Power).

**Updates** come through Yorik itself: Settings → System → Updates.

**By hand**, in PowerShell:

```
cd $env:LOCALAPPDATA\Yorik
docker compose ps
docker compose logs -f yorik
docker compose stop
docker compose up -d
```

**Removing Yorik:** `docker compose down -v` in that folder deletes Yorik
and all its data (the backups folder stays), then delete the folder.

## Windows as a client

Against a Yorik on another machine, nothing needs installing:

- Yorik in the browser (`http://<server>:8000`).
- [Dictate](https://github.com/winidi/dictate) with the *Yorik* provider
  sends recordings to the server; no model on the laptop.
- Hermes or any MCP client connects to `http://<server>:8000/mcp` with a
  personal token, see [MCP.md](MCP.md).
