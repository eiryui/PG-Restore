<p align="center">
  <img src="assets/white-orginal.png" alt="PG Restore logo" width="360">
</p>

<h1 align="center">PG Restore</h1>

<p align="center">
  Portable Windows desktop app to restore PostgreSQL databases from <code>.sql</code>, <code>.sql.gz</code> and <code>.dump</code> backups
  into local or cloud databases like Supabase, Neon and Aiven. No Python or PostgreSQL install needed.
</p>

<p align="center"><sub>Made by AziF</sub></p>

<!-- Add a screenshot at docs/screenshot.png and uncomment the next line:
![PG Restore screenshot](docs/screenshot.png)
-->

## What it does

Pick a backup file, paste your database URL, click **Restore database**. That's it.

- **Any common backup format**, detected automatically
- **Local or cloud databases**, with SSL and provider quirks handled for you
- **Safe by default**: the restore runs as a single transaction, so if anything fails your database is left unchanged
- **Standalone**: the app bundles its own runtime and PostgreSQL client tools, so nothing has to be installed on the PC that runs it
- **Live log and clear errors**: the failure dialog quotes the first real error and explains what it usually means

## Supported backups

| Backup | Detected as | Restored with |
| --- | --- | --- |
| `.sql` plain-text dump | plain SQL | `psql` |
| `.sql.gz` compressed plain-text dump | gzip SQL | `psql` (decompressed on the fly) |
| `.dump` / `.backup` (`pg_dump -Fc`) | custom | `pg_restore` |
| `.tar` (`pg_dump -Ft`) | tar | `pg_restore` |
| a folder (`pg_dump -Fd`) | directory | `pg_restore` |

## Using the app

1. **Backup file**: click *Browse file…* (or *Folder…* for a directory-format dump).
2. **Target database**: paste a URL such as `postgresql://user:password@host:5432/dbname`. The password is hidden until you tick *Show*.
3. Optionally click **Test connection** first.
4. Click **Restore database** and confirm.

### Options

| Option | What it does |
| --- | --- |
| Drop existing objects before restoring | Cleans existing objects first. Works for dump files (`pg_restore`) only, not plain `.sql`. |
| Cloud-friendly restore | Skips things managed databases don't allow: owner and permission statements, extension comments, the built-in `public` schema. Ticked automatically for remote hosts. |
| Ignore errors and continue | Keeps going when a statement fails (changes are **not** rolled back). Ends with "Finished with errors" and a count. |
| Copy log | Copies the whole log to the clipboard, handy for bug reports. |
| Cancel | Stops the restore. In the default mode nothing is committed. |

## Remote and cloud databases

For remote hosts the app:

- adds `sslmode=require` when your URL doesn't set an SSL mode (an existing `sslmode` is respected)
- passes the password through the environment instead of the command line, so it isn't visible in the process list
- retries the connection test, because some providers (for example Neon) sleep when idle
- recognises popular providers and warns about connection strings that break restores

| Provider | Notes |
| --- | --- |
| **Neon** | Use the **direct** connection (host without `-pooler`) for restores. |
| **Supabase** | Use the **Session pooler** (port 5432) or the direct connection, not the transaction pooler (port 6543). The direct host `db.<ref>.supabase.co` is IPv6-only unless you have the IPv4 add-on. |
| **Aiven** | SSL is required. For certificate checking add `sslmode=verify-full&sslrootcert=C:\path\ca.pem` to the URL. |
| **Prisma Postgres**, **Render**, **Railway**, **AWS RDS**, **Azure**, **DigitalOcean** | Detected. Use the direct (non-pooled) connection string. |

Other hosted PostgreSQL services work the same way as long as they accept a normal PostgreSQL connection. Not every provider has been tested, so please open an issue if you hit a problem.

## Build the standalone app

You need a Windows PC with Python 3 and tkinter (the standard python.org installer has both; the project was built with Python 3.14). Python is only needed to **build**, not to run the result.

1. **Get the PostgreSQL client tools.** Copy `psql.exe`, `pg_restore.exe` and **all** the `.dll` files from a PostgreSQL `bin` folder into `pgsql\bin`.
   For example `C:\Program Files\PostgreSQL\18\bin`, or the "binaries" zip from [EnterpriseDB](https://www.enterprisedb.com/download-postgresql-binaries).
   Use the same version as your newest database, or newer: `pg_restore` can't read dumps made by a newer version than itself.
2. **Python (optional portable copy).** If you don't want to use the Python installed on your PC, put a portable Python that includes tkinter and pip in a folder named `python` next to the build script. The "embeddable zip" from python.org does not include tkinter.
3. **Run `build_standalone.bat`.**
4. Your app is in `dist\PG Restore\` (run `PG Restore.exe`), and `PG Restore.zip` is ready to share. Unzip it anywhere, even on a USB stick.

The first time you run an unsigned exe, Windows SmartScreen may show an "unknown publisher" warning. Click **More info → Run anyway**.

### Run from source

```
python pg_restore_gui.py
```

The tools are looked up in `pgsql\bin` next to the script first, then on your `PATH`.

## Troubleshooting

| Problem | Usually means |
| --- | --- |
| `Restore failed (exit code 3)` | `psql` hit an error in a plain `.sql` file. The dialog shows the first error; the log has the rest. |
| `relation ... already exists` | The target already has these objects. Restore into an empty database, or tick *Ignore errors and continue* (for dump files you can also tick *Drop existing objects*). |
| `role ... does not exist` | The backup assigns ownership to a user that isn't on the target. Tick *Cloud-friendly restore* or *Ignore errors and continue*. |
| `unrecognized configuration parameter "transaction_timeout"` | The backup came from a newer PostgreSQL than the target server. Harmless: tick *Ignore errors and continue*. |
| `password authentication failed` | Wrong user or password. Copy the connection string again from your provider's dashboard. |
| `could not translate host name` | Host not found. For Supabase's direct host on an IPv4 network, use the Session pooler string. |
| Password with special characters | Prefer percent-encoding in the URL (`@` → `%40`, `#` → `%23`, `/` → `%2F`, `:` → `%3A`). |

## Project layout

```
pg_restore_gui.py        the app (Python + tkinter)
build_standalone.bat     builds the standalone folder and zip
assets/                  app icon and logo files
pgsql/bin/               PostgreSQL client tools (you provide these)
```

Don't commit the contents of `pgsql\bin`, `build` or `dist` to Git.

---

Made by AziF.
