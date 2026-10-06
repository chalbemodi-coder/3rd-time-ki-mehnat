[![Stars](https://img.shields.io/github/stars/your-username/AutoAnimeBot?style=flat-square&color=yellow)](https://github.com/your-username/AutoAnimeBot/stargazers)
[![Forks](https://img.shields.io/github/forks/your-username/AutoAnimeBot?style=flat-square&color=orange)](https://github.com/your-username/AutoAnimeBotfork)
[![Python](https://img.shields.io/badge/Python-v3.12.3-blue)](https://www.python.org/)
[![CodeFactor](https://www.codefactor.io/repository/github/your-username/autoanimebot/badge)](https://www.codefactor.io/repository/github/your-username/autoanimebot)
[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://github.com/your-username/AutoAnimeBot/graphs/commit-activity)
[![Contributors](https://img.shields.io/github/contributors/your-username/AutoAnimeBot?style=flat-square&color=green)](https://github.com/your-username/AutoAnimeBot/graphs/contributors)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](https://makeapullrequest.com)
[![License](https://img.shields.io/badge/license-GPLv3-blue)](https://github.com/your-username/AutoAnimeBot/blob/main/LICENSE)   
[![Sparkline](https://stars.medv.io/your-username/AutoAnimeBot.svg)](https://stars.medv.io/your-username/AutoAnimeBot)

## Developer Note

- __This repository is not intended or supported for deployment on KOYEB.__
- If Hosted On Heroku Then Make Sure You Are Using Premium Dynos Or Any Above then basic dynos.
- If You Don't Have High End VPS like **8vcpu or 32GiB RAM** So Don't Deploy This Bot.
- You Can Customize FFMPEG Code If You Know What You Are Doing.
- __Ensure that you have adhered to this developer note before reporting any errors.__

## Changelog Of Latest Update

### v0.1
- Shifted To Mongo Database.
- Changed Hashing Algo To SHA256.
- Added About Command.
- Added SS & MediaInfo On/Off
- Added Separate Anime Channel Upload
- <details><summary>Click Here To See How Separate Anime Channel Upload Look.</summary><img src="https://graph.org/file/a0636332545730a4d3d43.jpg" alt="sepul1"/><img src="https://graph.org/file/3eb0b86609469f385f4b5.jpg" alt="sepul2"/></details>
- Added Button Upload Support (File Store)
- <details><summary>Click Here To See How Button Upload Look.</summary><img src="https://graph.org/file/3e9abc9ec7de6a26fd1a1.jpg" alt="btnul"/></details>
- Added Multi Thread Encoding
- Added Progress Bar of Encoding
- Added Option For Logs In Main Channel
- Added ForceSub
- Added 480p Support
- Added Broadcast
- Major Modification In FFMPEG Code.
- Modified Anime Searcher
- Admin Panel Fixed
- ReWritten Whole Program (Fully OOPs Based)
- Optimized Core
- Added Heroku Support
- Added Custom CRF Support

## Contributing

- Any Sort of Contributions are Welcomed!
- Try To Resove Any Task From ToDo List Or Raise A Issue!

## How to deploy?
<p><a href="https://www.youtube.com/live/hWf7DN3nN_c"> <img src="https://img.shields.io/badge/See%20Video-black?style=for-the-badge&logo=YouTube" width="160""/></a></p>

### Fork Repo Then click on below button of ur fork repo.
[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy)

## Developer Note

- If Hosted On Heroku Then Encoding Of Per Episode Will Take Around 20mins.
- If You Don't Have High End VPS like 8vcpu or 32GiB RAM So Don't Deploy This Bot.
- You Can Customize FFMPEG Code If You Know What You Are Doing.

## Environmental Variable

### REQUIRED VARIABLES

- `BOT_TOKEN` - Get This From @Botfather In Telegram.

- `MONGO_SRV` - Get This From mongodb.com .

- `MAIN_CHANNEL` - ID of Channel Where Anime Will Upload.

- `CLOUD_CHANNEL` - ID of Channel Where Samples And Screenshots Of Anime Will Be Uploaded.

- `LOG_CHANNEL` - ID of Channel Where Status Of Proccesses Will Be Shown.

- `OWNER` - ID of Owner.

### OPTIONAL VARIABLES

- `SESSION` - Telethon Session String Of Your Telegram Account.

- `BACKUP_CHANNEL` - ID of Channel Where Anime Will Be Saved As BackUP if You Are Using Button Upload Option Then Make Sure To SET Backup Channel.

- `FORCESUB_CHANNEL` - ID of Channel Where You Want The User To Join (Make Sure You Promoted The Bot in that channel).

- `FORCESUB_CHANNEL_LINK` - Link of Channel Via User Join The `FORCESUB_CHANNEL`.

- `THUMBNAIL` - JPG/PNG Link of Thumbnail FIle.

- `FFMPEG` - You Can Set Custom Path Of ffmpeg if u want, default is `ffmpeg`.

- `LOG_ON_MAIN` - `True/False` It Will Send LOGS in `MAIN_CHANNEL` rather than `LOG_CHANNEL`, default is `False`

- `SEND_SCHEDULE` - `True/False` Send Schedule of Upcoming Anime of that day at 00:30 **IST**, default is `False`.

- `RESTART_EVERDAY` - `True/False` It Will Restart The Bot Everyday At 00:30 **IST**, default is `True`.

- `DELETE_FILES_FROM_PMS` - `True/False` It Will delete the file from pm of user after 10mins if button upload is enabled. default is `True`.

- `CRF` - Less CRF == High Quality, More Size , More CRF == Low Quality, Less Size, CRF Range = 20-51.
- `NYAA_FEED_URL` - Nyaa RSS feed for Hindi-audio releases (default: `q=hindi&c=1_2&f=0`).
- `NYAA_CHECK_INTERVAL` - Nyaa polling interval in seconds; default is `300`.

### Nyaa source behavior
The bot uses Nyaa's English-translated RSS feed with a Hindi filter, accepts Hindi Audio/Hindi Dub/Multi-Audio titles, skips batch/raw-only/subtitle-only releases, and requires an unprocessed 480p release to start, then downloads qualities in this order: 480p, 720p, 1080p, and optionally 2160p/4K. The torrent info-hash is stored in MongoDB to prevent repeat downloads.

## Deployment In VPS

- `git clone https://github.com/your-username/AutoAnimeBot.git`

- `nano .env` configure env as per [this](https://github.com/your-username/AutoAnimeBot/blob/main/.sample.env) or  using [this](https://github.com/your-username/AutoAnimeBot/blob/main/auto_env_gen.py).

- `sudo docker build . -t ongoing` (make sure to install docker first using `sudo apt install docker.io`)

- `sudo docker run ongoing`

## Commands

[![Comand](https://files.catbox.moe/utcf3f.jpg)](https://github.com/your-username/AutoAnimeBot/)

### Owner-only Telegram session login
The user session can be logged in from the owner's private chat without putting the session string in GitHub:
- `/sessionlogin` — asks for phone number, Telegram login code, and 2FA password privately.
- `/sessionstatus` — checks the connection.
- `/sessionlogout` — disconnects and removes the local session file.
These handlers are private and owner-only; they are not added to any public bot command menu. Phone/code/2FA input messages are deleted immediately when possible. The saved `.telegram_session` file is protected with mode `600` and ignored by Git. Never commit it or your `.env`.

### Dynamic ForceSub commands
These commands work in the bot owner private chat:
- `/addfsub -1001234567890` — add a channel; an optional fallback invite link may follow the ID.
- `/delfsub -1001234567890` — remove/disable a channel.
- `/listfsub` — list active ForceSub channels.
The bot creates a fresh join-request invite for each user and it expires after 10 minutes. The bot must be an administrator in every ForceSub channel.

**Uploading of Ongoing Animes Is Automatic**

<!-- ## About

- This Bot Is Currently Running In [This Channel](https://t.me/+q_OBZiXjkBFkYzk0) . -->


