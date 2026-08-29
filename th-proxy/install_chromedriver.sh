#!/bin/bash
# 预装与系统 Chrome 匹配的 chromedriver (避免 uc 运行时并发下载冲突)
set -e
CHROME_VER=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+\.\d+')
MAJOR=$(echo "$CHROME_VER" | cut -d. -f1)
echo "Chrome 版本: $CHROME_VER (major=$MAJOR)"

curl -fsSL "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json" -o /tmp/cf.json

DRIVER_URL=$(python3 - <<PYEOF
import json
d = json.load(open('/tmp/cf.json'))
major = "$MAJOR"
for ch in d['channels'].values():
    if ch['version'].split('.')[0] == major:
        for dl in ch['downloads'].get('chromedriver', []):
            if dl['platform'] == 'linux64':
                print(dl['url'])
                break
        break
PYEOF
)
echo "Driver URL: $DRIVER_URL"

curl -fsSL "$DRIVER_URL" -o /tmp/cd.zip
unzip -o /tmp/cd.zip -d /tmp/cd_extract
mv /tmp/cd_extract/*/chromedriver /usr/local/bin/chromedriver
chmod +x /usr/local/bin/chromedriver
rm -rf /tmp/cd.zip /tmp/cd_extract /tmp/cf.json
echo "chromedriver 已安装: $(chromedriver --version)"
