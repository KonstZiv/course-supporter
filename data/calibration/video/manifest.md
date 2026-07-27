# Video calibration material — manifest

Local calibration videos for the vision-model spike (presentation/video Pass 1
ladder calibration, pre-plan parts 1–2).

Binaries live in this folder **locally only** — they are gitignored and never
committed (see `.gitignore`: `data/calibration/video/*` with a `manifest.md`
exception). This file is the tracked index describing what each local file is,
so the calibration run is reproducible from the manifest even though the media
itself is not in git.

Requirements per the pre-plan: Ukrainian language, code or slides in frame,
moderate duration. Operator drops files here and records one row each.

| filename | source | duration | language | resolution | notes |
|----------|--------|----------|----------|------------|-------|
| _example.mp4_ | _YouTube URL / origin_ | _mm:ss_ | _uk / en / …_ | _e.g. 1920×1080_ | _code/slides in frame, etc._ |
