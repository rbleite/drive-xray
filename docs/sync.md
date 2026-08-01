# Using drive-xray on more than one machine

Store all `.db` index files in a shared cloud folder so every machine
sees every drive -- even offline.

1. Open the UI → sidebar → **⚙️ Configurações / Settings**
2. Set the folder to your local OneDrive/GDrive path (e.g. `C:\Users\you\OneDrive`)
3. Click **Import .db files from this folder** to pick up indexes synced from other machines
4. New indexes created on this machine go there automatically

Mount points are resolved automatically across machines and operating
systems: a drive indexed on macOS at `/Volumes/MyDisk` is recognized when
plugged into Windows (`E:\`) or Linux (`/media/<user>/MyDisk`) — the app
matches the drive's actual content (its top-level entries) against the
mounted volumes, so verify, refresh, dedupe and delete keep working
wherever the disk shows up.

