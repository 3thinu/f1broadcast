# Pit Wall

A countdown to the next session, a replay of the latest race, and live timing with every car on the track map.

## Website (GitHub Pages)

1. Upload these files to a new GitHub repository, on the `main` branch. Include the `.github` folder.
2. In the repository, open **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Open the **Actions** tab, choose **Build and deploy**, and click **Run workflow**.

After a few minutes the site is live at `https://YOUR-NAME.github.io/REPOSITORY/`. It rebuilds itself every Monday and Thursday, so the replay moves to each new race automatically.

The website shows the countdown and the replay. Live timing needs the bridge below.

## Live timing on your computer

Requires Python 3.10 or newer.

```
pip install -r requirements.txt
python build_data.py
python bridge.py
```

Open http://localhost:8765. The countdown runs until a session starts, then the timing screen opens by itself.

`build_data.py` downloads the schedule, the circuit maps and the latest race for the replay. Run it again after each race weekend.

## How it works

- `index.html` is the whole site: countdown, replay and timing screen.
- `build_data.py` uses [FastF1](https://github.com/theOehrly/Fast-F1) to save `data/schedule.json`, `data/tracks/*.json` and `data/replay.json`.
- `bridge.py` reads the free streams of the official live timing feed and passes them to the page. During live sessions, car positions are estimated from mini-sector timing, because the GPS stream needs an F1 TV subscription. Replays use the real car positions.

## Notes

Unofficial fan project for personal use. Not affiliated with Formula 1, the FIA or any team. Formula 1 can change its live timing feed at any time, which can break live mode until the bridge is updated.

MIT licence.
