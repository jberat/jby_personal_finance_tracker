# Install Guide

There are two ways to install Personal Financial Tracker: hand the job to an AI assistant (recommended, five minutes, works even if you've never opened a terminal), or do it by hand with the manual steps further down.

Either way you'll need **Python 3.10 or newer** on your machine. If you don't have it, your AI assistant can walk you through that too — just tell it, and it will fold the Python install into the steps below.

## The fast way: ask an AI assistant

If you use any AI assistant that can run commands on your computer (or walk you through running them), copy the entire block below, fill in the one blank at the top, and paste it into a new conversation.

```text
I want you to install a local web app called Personal Financial Tracker
on my computer. Here's everything you need to know.

The code lives at: <PASTE THE REPOSITORY URL OR ZIP DOWNLOAD LINK HERE>

My operating system: <macOS / Windows / Linux>

Please do the following, explaining each step as you go. If you can run
commands directly, run them; if not, give me the exact commands to paste
and wait for me to report back after each one.

1. Check that Python 3.10+ is installed (python3 --version, or
   python --version on Windows). If it isn't, help me install it first.

2. Clone or download the repository into a sensible folder in my home
   directory, e.g. ~/personal-financial-tracker (or
   %USERPROFILE%\personal-financial-tracker on Windows).

3. Inside that folder, create a Python virtual environment and activate it:
   - macOS/Linux:  python3 -m venv venv  then  source venv/bin/activate
   - Windows:      python -m venv venv   then  venv\Scripts\activate

4. Install the dependencies:  pip install -r requirements.txt
   If anything fails to install, read the error and fix it (common fixes:
   upgrade pip, or install a system package the error names) rather than
   skipping the dependency.

5. Start the app:  python3 app.py  (python app.py on Windows)

6. Verify it's running: the terminal should say it's serving on
   http://127.0.0.1:5005. Open that address in my browser and confirm the
   app loads. Log in with the temporary password `changeme` (I will
   change it right away from Docs & Settings → Security inside the app).

7. Tell me how to stop the app (Ctrl+C in the terminal) and how to start
   it again later (activate the venv, then python3 app.py), and write
   those two commands down for me in a short note.

Important constraints:
- This app is local-only and must stay that way. Do not expose it to the
  network, change the host/port, or install anything beyond
  requirements.txt.
- Do not modify any of the app's code during installation.
```

That's it. The assistant will handle the rest, and you'll finish at the login screen.

## The manual way

### macOS

1. **Check Python.** Open Terminal (Applications → Utilities → Terminal) and run `python3 --version`. You want 3.10 or newer. If the command isn't found, install Python from [python.org](https://www.python.org/downloads/) or via Homebrew (`brew install python`).

2. **Get the code.** Either:
   ```bash
   git clone https://github.com/jberat/jby_personal_finance_tracker.git ~/personal-financial-tracker
   ```
   or download the ZIP from the repository page, unzip it, and move the folder somewhere sensible like your home directory.

3. **Create and activate a virtual environment.** This keeps the app's Python packages isolated from everything else on your machine:
   ```bash
   cd ~/personal-financial-tracker
   python3 -m venv venv
   source venv/bin/activate
   ```
   Your prompt will now start with `(venv)`.

4. **Install dependencies.**
   ```bash
   pip install -r requirements.txt
   ```

5. **Run it.**
   ```bash
   python3 app.py
   ```
   Leave this terminal window open — it *is* the app. Open **http://127.0.0.1:5005** in your browser.

### Windows

1. **Check Python.** Open Command Prompt or PowerShell and run `python --version`. If it's missing or old, install from [python.org](https://www.python.org/downloads/) — and on the installer's first screen, check the box that says **"Add Python to PATH"**.

2. **Get the code.** Clone with git if you have it, or download and extract the ZIP to something like `C:\Users\<you>\personal-financial-tracker`.

3. **Create and activate a virtual environment.**
   ```bat
   cd %USERPROFILE%\personal-financial-tracker
   python -m venv venv
   venv\Scripts\activate
   ```

4. **Install dependencies.**
   ```bat
   pip install -r requirements.txt
   ```

5. **Run it.**
   ```bat
   python app.py
   ```
   Open **http://127.0.0.1:5005** in your browser.

### Starting and stopping, forever after

- **Stop:** press `Ctrl+C` in the terminal running the app, or just close that terminal.
- **Start:** open a terminal, `cd` into the app folder, activate the venv (`source venv/bin/activate` on macOS/Linux, `venv\Scripts\activate` on Windows), then `python3 app.py`. Two commands, ten seconds.
- **macOS/Linux shortcuts:** the repo ships `./start.sh` (starts in the background, logs to `app.log`, opens your browser) and `./restart.sh` (force-kills anything on port 5005, then starts fresh). Note these find their own Python and don't use the venv.

The app only runs while that terminal session is alive. Your data is safe either way — everything is written to `finance.db` as you go, so stopping the app never loses anything.

## What to expect on first launch

The first time you run the app:

- **The database is created automatically.** A new, empty `finance.db` appears in the app folder. No setup step needed.
- **Log in with the temporary password `changeme`**, then change it immediately in Docs & Settings → Security (or write your new password into the `.app_password` file next to `app.py` and restart). This login protects the app from anyone else using your computer; it is not tied to any online account.
- **Starter categories are pre-seeded.** A generic two-level category tree (things like Food & Dining → Groceries, Transportation → Gas) is loaded for you. You can rename, add, or remove categories later — see [customize_with_ai.md](customize_with_ai.md).
- **Everything else is empty.** The dashboard will look bare until you import your first bank CSV. That's normal. Head to the import page, feed it a statement, and work through the review queue.

## Troubleshooting

- **"Address already in use" when starting.** Something else (probably a previous copy of the app you forgot about) is already on port 5005. On macOS/Linux, `./restart.sh` fixes this (it kills anything on the port first). Otherwise find that old terminal and Ctrl+C it, or restart your computer. To check what's actually running, open `http://127.0.0.1:5005/health` — it shows the running process's start time and PID.
- **Browser says "can't connect" at 127.0.0.1:5005.** The app isn't running. Check the terminal — if it shows an error, copy the whole error into your AI assistant and ask it to diagnose.
- **`pip install` fails.** Copy the full error message to your AI assistant. Nine times out of ten the fix is `pip install --upgrade pip` and retrying.
- **Anything else.** Paste the error and a description of what you did into your AI assistant. These are ordinary Python problems and AI assistants are extremely good at them.
