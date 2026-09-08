# Yorik on Windows (WSL2)

Yorik is a Linux server. On Windows it runs inside WSL2, the Ubuntu that
Docker Desktop uses anyway. Nothing about the install changes; what
changes is the box around it. This page lists the four things that trip
people up and the one honest caveat.

**The caveat first:** a gaming PC that is off at night is not a home
server. Calendar reminders, the WhatsApp bridge and email fetching want
to run around the clock. WSL2 is the right way to *try* Yorik on the PC
you already have. For the family, a used mini-PC or an old laptop with
Ubuntu is the setup the rest of the docs assume, and the Windows PC then
uses Yorik in the browser.

## Install

1. **WSL2 with Ubuntu.** In PowerShell as administrator:
   ```
   wsl --install -d Ubuntu-24.04
   ```
   Reboot, open "Ubuntu" from the Start menu, pick a user name.
2. **Docker Desktop** with the WSL2 backend (default). In Docker Desktop →
   Settings → Resources → WSL integration, enable it for `Ubuntu-24.04`.
3. In the Ubuntu window, follow [INSTALL.md](INSTALL.md) from step 1. The
   installer sees an Ubuntu with Docker and does what it always does.

## The four stumbling blocks

### 1. Memory

WSL2 takes half of the machine's RAM by default. With 16 GB that is 8 GB
for Ubuntu *and* Docker together, which is tight for Supabase + Immich +
Paperless. Create `C:\Users\<you>\.wslconfig`:

```
[wsl2]
memory=12GB
swap=4GB
```

then `wsl --shutdown` in PowerShell and reopen Ubuntu.

### 2. Where the data lives

Keep Yorik and its `data/` directory on the Linux side (`~/yorik-ai`),
never under `/mnt/c/...`. Files on the Windows drive go through a
translation layer that makes Immich's photo scanning and Paperless OCR
many times slower.

### 3. Reaching Yorik from phones and tablets

WSL2 has its own network. Two ways out:

- **Windows 11 22H2 or newer:** add to `.wslconfig`
  ```
  [wsl2]
  networkingMode=mirrored
  ```
  Ubuntu then shares the PC's address and `http://<pc-name>:8000` works
  from any device on the Wi-Fi. Allow port 8000 in Windows Defender
  Firewall (inbound rule, TCP 8000).
- **Older Windows:** forward the port in PowerShell as administrator,
  once per boot or as a scheduled task:
  ```
  netsh interface portproxy add v4tov4 listenport=8000 listenaddress=0.0.0.0 connectport=8000 connectaddress=$(wsl hostname -I)
  ```

### 4. Starting at boot

WSL2 does not start services when Windows boots. Options, best first:

- Enable systemd in WSL (`/etc/wsl.conf`, `[boot] systemd=true`, then
  `wsl --shutdown`). The `yorik` unit the installer creates then behaves
  as on a real Ubuntu, as soon as any WSL shell has been opened once.
- Task Scheduler: a task at logon that runs `wsl -d Ubuntu-24.04 -u <you> -- sudo systemctl start yorik`.

Also switch off "fast startup" and set the PC to never sleep while you
rely on it.

## Windows as a client

Everything a Windows user usually wants works without WSL:

- Yorik in the browser (`http://<server>:8000`).
- [Dictate](https://github.com/winidi/dictate) with the *Yorik* provider
  sends recordings to the server; no model on the laptop.
- Hermes or any MCP client connects to `http://<server>:8000/mcp` with a
  personal token, see [MCP.md](MCP.md).

## Not supported

A native Windows install without WSL2. The containers Yorik bundles are
Linux images; a PowerShell installer would end up starting the same
Ubuntu underneath.
