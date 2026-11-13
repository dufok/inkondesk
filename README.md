# inkondesk

> **TL;DR (EN):** an old jailbroken **Kindle Paperwhite 1** turned into a USB e-ink side screen for the desk.
> I use it as an **info display** next to my XP-Pen Shortcut Remote: it shows what the remote's keys do in the
> active app, has touch buttons that press keys on the PC, and shows a screensaver when the PC is off.
> The same setup can be adapted into a **Stream Deck–style touch hotkey pad**.

![Экран на Kindle: карта пульта XP-Pen слева, цифровой блок справа](assets/preview-map.png)

## Что это

Старый **Kindle Paperwhite 1** (2012, прошивка 5.6.1.1, джейлбрейк) подключён к ПК одним USB-кабелем и стоит
на столе как постоянный e-ink экран. Я использую его как **инфо-дисплей** рядом с пультом
**XP-Pen Shortcut Remote (ACK05)**:

- **слева** — подсказки, что делают клавиши K1–K10 пульта в программе, которая сейчас активна. Подписи берутся
  прямо из настроек драйвера XP-Pen, профиль переключается сам по активному окну Windows (Blender, Krita, …) —
  больше не надо лазить в меню драйвера;
- **справа** — свои сенсорные кнопки, пока цифровой блок 0–9: тап по цифре → ПК нажимает клавишу NumPad
  в активной программе (в Blender это виды 1/3/7, 5 — перспектива/орто);
- **снизу** — подсветка `−` / `+` / `выкл`;
- **ПК выключен или уснул** — через минуту на Kindle появляется заставка, подсветка гаснет; ПК вернулся —
  экран возвращается сам.

E-ink для этого хорош: не светит в глаза, читается при любом свете, картинка держится даже без питания.

![Заставка, когда ПК выключен](assets/preview-screensaver.png)

## Можно сделать свой Stream Deck

Вся связка уже умеет главное, что нужно пульту с экранными кнопками: **нарисовать кнопки → поймать тап →
выполнить действие на ПК**. Поэтому экран можно полностью переделать под **Stream Deck–подобное устройство**:

- убрать карту XP-Pen и отдать весь экран 758×1024 под сетку своих кнопок (например 4×5 по ~180 px — палец
  попадает с точностью ±30 px, так что кнопки от 120 px работают надёжно);
- на каждую кнопку повесить что угодно: сочетание клавиш (`Ctrl+Shift+…`), строку текста, запуск программы,
  скрипт, HTTP-запрос к Home Assistant — это просто Python на ПК;
- держать разные наборы кнопок для разных программ — переключение по активному окну уже есть;
- несколько страниц кнопок с листанием тапом.

Где это в коде (`xppen_kindle.py`):

| Что | Где |
|---|---|
| список экранных кнопок | `NUMPAD` (подпись + код клавиши) |
| как кнопки рисуются и куда тапать | `draw_keypad()` → добавляет `hits` с действием `("key", vk, label)` |
| что происходит по тапу | обработка `action[0] == "key"` в `run_kindle()` → `send_key(vk)` |
| нажатие клавиши в Windows | `send_key()` — `SendInput`; для сочетаний — нажать модификаторы, клавишу, отпустить в обратном порядке |
| раскладка экрана | `render()`, константы `MAP_X1`, `PAD_X0` |

Чего e-ink не даст по сравнению со Stream Deck: цветных иконок (только 16 оттенков серого) и мгновенного
отклика картинки (перерисовка ~0.3–1 с; само нажатие на ПК уходит сразу).

## Как устроено

```
 Windows ПК                                          Kindle Paperwhite 1 (Linux, джейлбрейк)
 ─────────                                           ──────────────────────────────────────
 xppen_kindle.py (pythonw, в фоне)                   USBNetwork: сеть по USB + SSH (dropbear)
  • читает %APPDATA%\XPPen\config.xml   ── PNG ──▶   fbink рисует картинку на e-ink
  • следит за активным окном            ◀─ тапы ──   cat /dev/input/event0 (тачскрин)
  • рисует экран (Pillow, 1024×758)     ─ heartbeat▶ watchdog.sh: нет сигнала 60 с → заставка
  • SendInput: клавиши в активное окно
         192.168.15.201  ◀──── USB (RNDIS) ────▶  192.168.15.244
```

- Kindle на время работы переводится в полноэкранный режим: выключаются его интерфейс (`framework`),
  верхняя панель (`pillow`) и оконный менеджер (`awesome`), иначе они рисуют себя поверх и ловят тапы.
- Программа переживает отключения: ждёт Kindle, после его перезагрузки снова забирает экран, ошибки пишет
  в `xppen_kindle.log` и работает дальше; вторая копия сама закрывается.
- Подсветка: `lipc-set-prop com.lab126.powerd flIntensity N`; на 0 powerd всё равно светит минимумом,
  поэтому настоящее «выкл» пишется прямо в драйвер (`fl_intensity`).

## Как повторить

> ⚠️ Джейлбрейк и откат прошивки — на свой страх и риск. Шаги ниже — для **Paperwhite 1 на 5.6.1.1**,
> на других моделях и прошивках путь другой (см. [kindlemodding.org](https://kindlemodding.org/)).

1. **Джейлбрейк PW1 5.6.1.1:** откат на 5.3.3 официальным `update_kindle_5.3.3.bin` (скопировать в корень, не
   отключая кабель держать кнопку питания до перезагрузки) → K5 jailbreak (`kindle-5.4-jailbreak.zip`,
   «Обновить Kindle») → Universal Hotfix → обратно на 5.6.1.1 → снова «Run Hotfix».
   Подробности: [MobileRead: downgrade PW1 5.6.1.1 → 5.3.3](https://www.mobileread.com/forums/showthread.php?t=264432).
2. **KUAL + MRPI + USBNetwork** (пакеты от NiLuJe, [MobileRead snapshots](https://www.mobileread.com/forums/showthread.php?t=225030));
   свой публичный SSH-ключ → `usbnet/etc/authorized_keys` (LF!); `usbnet/DISABLED_auto` → `usbnet/auto`
   (сеть по USB сразу при загрузке).
3. **DHCP на Kindle** (чтобы любой USB-порт работал без ручного IP): `kindle-scripts/udhcpd.conf` →
   `usbnet/etc/`, применить `kindle-scripts/usbnetwork.patch` к `usbnet/bin/usbnetwork`.
4. **Драйвер в Windows 11:** Kindle в режиме сети определяется как COM-порт. Нужен подписанный драйвер
   «Acer Incorporated. – Other hardware – USB Ethernet/RNDIS Gadget» для `USB\VID_0525&PID_A4A2` из
   [Microsoft Update Catalog](https://www.catalog.update.microsoft.com/Search.aspx?q=USB%5CVID_0525%26PID_A4A2),
   ставится `pnputil /add-driver RNDIS.inf /install` (от админа).
5. **ПК:** Python 3 + Pillow; приватный ключ положить в `kindle-files/ssh/kindle_ed25519`
   (`known_hosts` создастся рядом). Запуск — `kindle_start.cmd`, для автозапуска ярлык на него в `shell:startup`.

## Файлы

| Файл | Что это |
|---|---|
| `xppen_kindle.py` | вся программа на ПК |
| `kindle_start.cmd` / `kindle_stop.cmd` | запустить в фоне / остановить и вернуть Kindle обычный интерфейс |
| `test_screen.py` | первая тестовая картинка (часы, статус, полоска 16 оттенков серого) |
| `assets/badprompt.webp` | цветной логотип для заставки (ЧБ-версию собирает программа) |
| `assets/preview-*.png` | картинки для этого README |
| `kindle-scripts/watchdog.sh` | сторож заставки, ставится на Kindle в `/mnt/us/kindle-dash/` |
| `kindle-scripts/dash_on.sh`, `dash_off.sh` | вручную включить / выключить полноэкранный режим Kindle |
| `kindle-scripts/udhcpd.conf`, `usbnetwork.patch` | DHCP на Kindle: конфиг и патч к скрипту USBNetwork |

## Запуск

```bash
kindle_start.cmd                      # в фоне, без окна
kindle_stop.cmd                       # остановить, вернуть обычный Kindle
python xppen_kindle.py                # в консоли для отладки (Ctrl+C)
python xppen_kindle.py --preview out  # только отрендерить PNG всех профилей
```

## Чего нет в репозитории

`kindle-files/` не коммитится: там скачанные прошивки, джейлбрейк, хотфикс, KUAL, MRPI, USBNetwork, драйвер
(~500 МБ, всё можно скачать заново) и **приватный SSH-ключ**. Полные копии скрипта USBNetwork (код NiLuJe)
тоже не коммитятся — только `usbnetwork.patch`.

## Ограничения

- Только Windows (активное окно и `SendInput` через WinAPI); проверено на Kindle Paperwhite 1, FW 5.6.1.1.
- Windows не даёт обычной программе нажимать клавиши в окнах, запущенных от администратора.
- Если Kindle перезагрузится, пока ПК выключен, сторож заставки не запустится до включения ПК.

## Лицензия

Код — [MIT](LICENSE).

- Логотип **BadPrompt** (`assets/badprompt.webp` и картинка заставки, которая из него собирается) под лицензию
  не попадает — это не код, а знак автора; для своей заставки подставьте свою картинку.
- `kindle-scripts/usbnetwork.patch` меняет скрипт из USBNetwork от NiLuJe — на сам этот скрипт действуют условия
  его автора.
