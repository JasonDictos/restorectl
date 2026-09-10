# restorectl

Whole-machine [restic](https://restic.net) backups that you can actually see,
browse and trust — a nightly root-level backup engine, a CLI that tells you
the truth about your backup posture, a web browser for your snapshots, and a
KDE tray monitor.

Built because a backup you have never restored from is a hypothesis, not a
backup.

MIT licensed.

---

## What's here

| | |
|---|---|
| `bin/system-backup` | nightly whole-machine backup, runs as root, mails a report |
| `bin/restorectl` | the CLI: setup, schedule, status, browse, restore |
| `bin/capture-layout` | snapshots the disk/partition/LVM/ZFS layout *into* the backup |
| `bin/bare-metal-restore` | rebuilds a machine onto different hardware (WIP) |
| `web/restorectl-web.py` | read-only HTTP API over the repos |
| `web/index.html` | snapshot browser — snapshots by date, file tree, download |
| `web/restorectl-tray.py` | KDE/Qt tray monitor with live progress |

## Install

```bash
git clone https://github.com/JasonDictos/restorectl
cd restorectl
sudo ./install.sh            # auto-detects client vs backup server
restorectl setup             # creates/verifies repo, password, excludes
restorectl enable            # turn on the nightly timer
restorectl wtf               # prove it works
```

`install.sh --uninstall` removes the tooling and never touches your
repositories.

## The CLI

```
restorectl wtf                    what is my actual backup posture?
restorectl status                 one-screen summary
restorectl snapshots              list snapshots
restorectl tree [SNAP] [PATH]     tree view of a snapshot
restorectl get SNAP PATH [DEST]   restore a file or directory
restorectl diff SNAP PATH         diff a snapshot against the live file
restorectl mount [SNAP]           FUSE-mount snapshots read-only
restorectl run --watch            back up now, stream progress
restorectl schedule "*-*-* 02:00" change the nightly time
restorectl browse                 open the web UI
restorectl tray                   run the tray monitor
restorectl --from HOST ...        operate on another machine's repo
```

### `restorectl wtf`

The command this project exists for. It answers "am I actually protected?"
rather than "did the last job exit zero":

- does the repository open, and is the password the right one
- is the nightly timer enabled, when did it last run, did it succeed
- **which real filesystems are missing from the most recent snapshot**
- is the password escrowed somewhere that survives this machine dying
- is the repo somewhere the offsite sync actually covers

That third one catches the failure mode that matters: a backup job that is
green every night while silently omitting half the machine.

## Design notes

**"Differential" backups.** restic has no full/incremental/differential
modes and does not need them. Every snapshot is a complete logical view of
its paths; physically only new content-addressed blocks are written. You get
differential storage cost with full-restore convenience, and no chain to
replay — any snapshot stands alone.

**Root, not user.** The backup runs as a system unit because a user-level
job cannot read `/etc/shadow`, `/root`, or `/var/lib`. Worse, restic exits
**3** for "completed but some files were unreadable", so a user-level job
backing up `/` reports *success* while skipping most of the system. This
project deliberately does **not** list exit 3 in `SuccessExitStatus`.

**`--one-file-system`, plus explicit paths.** The backup names each real
filesystem (`/ /boot /boot/efi /home`) and refuses to cross mount
boundaries. That keeps it out of the NFS backup target, the container pool,
and every pseudo-filesystem, without depending on exclude rules being right.

**Read paths never lock.** Every read (`snapshots`, `ls`, `dump`, `stats`)
passes `--no-lock`. Without it, browsing blocks for the entire duration of a
running backup — which is exactly when someone wants to look.

**Password lookup is host-scoped.** `/etc/restic/password` is *this*
machine's key and is only consulted for the local host. Host-specific
escrowed keys are checked first, so a backup server browsing another
machine's repo doesn't try its own password and report "wrong password".

## The web UI

A read-only API (`restorectl-web.py`) plus a single-page browser. Runs on
the machine that holds the repositories, so it can see every host's backups
at once, binds to loopback, and is exposed through nginx.

- snapshots grouped by day, newest first
- click any snapshot to browse it as a file tree
- breadcrumbs, directory descent, per-file download straight out of restic

There is no endpoint that writes, forgets or prunes. But it does expose the
full contents of every backup, so the shipped nginx config turns on basic
auth by default:

```bash
sudo htpasswd -c /etc/nginx/restorectl.htpasswd youruser
```

The nginx snippet is a **complete `server` block on its own port**, because
`conf.d/*.conf` is included at `http` scope — a bare `location` there is a
syntax error that takes down every other vhost on the box.

## The tray monitor

PySide6 (ships with Fedora KDE, nothing to pip install). The icon *is* the
progress indicator: a filled disc when idle, a progress arc while a backup
runs, red when the last run failed or the timer is off. Click for live log
detail, right-click to run a backup, open the browser, or run `wtf`.

```bash
restorectl tray                                   run it
python3 restorectl-tray.py --autostart            start it at login
```

## Status

Working and in nightly use: backup engine, scheduling, mailed reports,
layout capture, CLI, web UI, tray.

In progress: `bare-metal-restore` (plans and dry-runs correctly, including
replanning onto a different disk count; the execute path is not yet
drill-verified), bootable recovery media, and an automated monthly restore
drill into a VM.
