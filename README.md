# Kindle — e-ink экран для пульта XP-Pen

Старый **Kindle Paperwhite 1** (2012, FW 5.6.1.1, джейлбрейк) подключён к ПК по USB и работает
постоянным e-ink экраном рядом с пультом **XP-Pen Shortcut Remote (ACK05)**:

- **слева** — подсказки, что делают клавиши K1–K10 пульта в активной программе (берутся из настроек
  драйвера XP-Pen, профиль переключается сам по активному окну Windows);
- **справа** — свои экранные кнопки, пока цифровой блок 0–9: тап → клавиша NumPad в активной программе;
- **снизу** — подсветка `−` / `+` / `выкл`;
- **ПК выключен / уснул** — через минуту на Kindle заставка BadPrompt, подсветка гаснет.

Связь — сеть по USB (USBNetwork, RNDIS): Kindle `192.168.15.244`, ПК `192.168.15.201`, SSH по ключу.
Картинку рисует Python (Pillow) на ПК, на Kindle её выводит `fbink`; касания читаются с `/dev/input/event0`.

## Файлы

| Файл | Что это |
|---|---|
| `xppen_kindle.py` | вся программа на ПК (запуск в фоне через `pythonw`) |
| `kindle_start.cmd` / `kindle_stop.cmd` | запустить в фоне / остановить и вернуть Kindle обычный интерфейс |
| `test_screen.py` | первая тестовая картинка (часы, статус, полоска серого) |
| `assets/badprompt.webp` | цветной логотип для заставки (ЧБ-версию собирает программа) |
| `kindle-scripts/watchdog.sh` | сторож заставки, ставится на Kindle в `/mnt/us/kindle-dash/` |
| `kindle-scripts/dash_on.sh`, `dash_off.sh` | вручную включить / выключить полноэкранный режим Kindle |
| `kindle-scripts/udhcpd.conf`, `usbnetwork.patch` | DHCP на Kindle: конфиг и наш патч к скрипту USBNetwork |

## Запуск

```bash
kindle_start.cmd                      # в фоне, без окна (ярлык на него лежит в shell:startup)
kindle_stop.cmd                       # остановить, вернуть обычный Kindle
python xppen_kindle.py                # в консоли для отладки (Ctrl+C)
python xppen_kindle.py --preview out  # только отрендерить PNG всех профилей
```

## Чего нет в репозитории

`kindle-files/` не коммитится (см. `.gitignore`): там ~500 МБ скачанного (прошивки 5.3.3 и 5.6.1.1,
джейлбрейк K5, хотфикс, KUAL, MRPI, USBNetwork, подписанный RNDIS-драйвер для Windows) и **приватный
SSH-ключ** `kindle-files/ssh/kindle_ed25519`. Полные копии скрипта USBNetwork (чужой код, NiLuJe) тоже
не коммитятся — только `usbnetwork.patch`.

Подробный журнал всего пути (джейлбрейк, драйвер, грабли, команды) — заметка в Obsidian
`Projects/📟 Kindle — e-ink экран по USB.md`.
