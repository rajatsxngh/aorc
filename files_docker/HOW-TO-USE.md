# How to use the AORC base image (plain-language guide)

This folder has one important file: **`Dockerfile`**. It's a recipe.
Docker reads it and builds a "sealed room" image that already has
Claude Code + Matt Pocock's skills inside. Every agent you run in a
container starts from this image, so the skills are always present.

You need **Docker Desktop** installed and running first.

---

## Step 1 — Build the image (do this once)

Open a terminal, go into this folder, and run:

    docker build -t aorc-base .

What it means:
- `docker build` = follow the recipe and make the image
- `-t aorc-base` = name the finished image "aorc-base" (t = tag/name)
- `.` = "the recipe is in this current folder"

This takes a few minutes the first time (it downloads Node, installs
Claude Code and the skills). When it's done, the image lives on your
machine, ready to reuse instantly.

---

## Step 2 — Run a container from it

To open a sealed room and look around inside:

    docker run -it aorc-base

- `docker run` = start a container from the image
- `-it` = interactive, so you get a terminal prompt inside
- You'll land at a bash prompt *inside the container*. Type `exit` to leave.

To run it with your API key AND share your project folder in:

    docker run -it \
      -e ANTHROPIC_API_KEY="sk-ant-...your-key..." \
      -v "$PWD":/work \
      aorc-base

- `-e ANTHROPIC_API_KEY="..."` = hand it your key safely (only while it runs)
- `-v "$PWD":/work` = share your current folder into the container's /work
  so the agent can see and edit your actual project files
- Replace `sk-ant-...your-key...` with your real key from
  console.anthropic.com. NEVER put the key inside the Dockerfile.

---

## Step 3 — Confirm the skills are baked in

Once inside the container (after `docker run -it aorc-base`), run:

    claude --version

That confirms Claude Code is present. The skills installed alongside it
during the build, so a fresh `claude` session in here has them too.

---

## When do you actually need this?

- **Building AORC by hand** (running /to-issues, /tdd to write AORC's
  code): you DON'T need this image — you run Claude Code directly on
  your machine, where your skills are already installed.

- **Running an unattended Ralph loop safely**, OR **AORC's own per-issue
  containers**: you DO need this image, because the sandbox is your
  safety boundary and containers don't inherit your machine's install.

So: build it now so it's ready, but you won't use it until you go
"away-from-keyboard" or start running AORC itself.
