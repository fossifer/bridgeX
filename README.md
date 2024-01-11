# BridgeX

Bridge messages between Telegram, IRC and Discord. Complete revamp of https://github.com/fossifer/LilyWhiteBot with more exciting features.

## Features

* Editing or deleting a message in one group will be reflected to other connected groups. Even IRC will get a notice.
* Enhanced stability significantly comparing to the nodeJS version.
* Upload long messages to (self hosted) pastebin for IRC users. No more truncates!
* Better support of Telegram multimedia message types.
* Directly upload media files to Discord rather than displaying a link.
* Powerful web interface with OAuth 2.0 to manage messages and hot reload configs. (WIP)

## Run

Tested with Python 3.9 or above.

```bash
git clone https://github.com/fossifer/bridgeX.git
cd bridgeX
pip install -r requirements.txt
mv filter-example.yaml filter.yaml
mv config-example.yaml config.yaml
# Don't forget to edit your config! You need to fill bot tokens and bridged groups.
nohup python main.py &
```
