# Menu bar app (macOS): how it runs

Reference for `qswap menubar`. Installing it and everyday use are in the [README](../README.md#menu-bar-macos).

## Lifecycle

`qswap menubar` hands the menu bar to launchd and returns: it keeps running after you close the terminal, comes back at login and is restarted if it crashes. Running the command again only tells you it is already up. *Quit* in the menu stops it until the next login. *Settings → Start at login* turns the login start off, after which `qswap menubar` runs it for the current session only. `qswap menubar --uninstall-service` removes it altogether, and `qswap menubar --foreground` runs it attached to the terminal instead, for debugging.

```bash
qswap menubar --service-status      # running? pid? starts at login? auto-switch on?
qswap menubar --install-service     # register the login start again and restart it now
qswap menubar --uninstall-service   # stop it and remove the LaunchAgent
qswap menubar --foreground          # run attached to this terminal, for debugging
```

## Auto-switch

Enable *Settings → Auto-switch accounts* to run the same engine as [`qswap auto`](../README.md#automatic-switching) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on: `qswap menubar --auto-switch` turns it on from the terminal (`--no-auto-switch` off), and both that command and `--service-status` say which it is. The menu's first row says whether auto-switch is running, off, or stopped (click it to restart) and when usage was last refreshed; the title carries a ⚠ while a stopped engine waits for that click.

## The wrapper app

`qswap menubar` runs from a small wrapper app, `/Applications/AI Quota Swap Menubar.app`, that it builds (and refreshes after upgrades) on every start. The wrapper gives it the bundle identifier `com.ai-quota-swap.menubar`, which menu bar managers (Dimble, Ice, Bartender, SaneBar) need to list, hide and order the icon. If `/Applications` is not writable the bundle lands in the data directory with a one-line warning; there, the first time a manager hides anything the icon goes down with it. Set `AI_QUOTA_SWAP_NO_BUNDLE=1` to run in the plain interpreter instead.

## Files

The plist lives at `~/Library/LaunchAgents/com.aiquotaswap.menubar.plist` while Start at login is on and in the data directory while it is off; logs go to `~/Library/Logs/com.aiquotaswap.menubar.{log,err}`.

## Upgrades

`qswap upgrade` restarts a running menu bar when the build changed; after an upgrade made some other way, the next `qswap menubar` notices the old build and restarts it.

## Environment

The service follows the `CLAUDE_CONFIG_DIR`, `CLAUDE_SECURESTORAGE_CONFIG_DIR` and `XDG_DATA_HOME` of the shell that set it up, and `--install-service` from a shell in another profile says what moved.

## Login Items & Extensions

If you switched it off under System Settings → General → Login Items & Extensions → Allow in the Background, `qswap menubar` still starts it when you ask, but says that macOS will not start it at login until you turn that back on.
