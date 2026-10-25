# Bridgething for Home Assistant

This app connects a Spotify Car Thing, flashed with [Bridgething](https://github.com/JoeyEamigh/bridgething), to Home Assistant. Plug the Car Thing into a USB port on the computer that runs Home Assistant. After you log in to Spotify, the screen shows the song Spotify is playing, including its cover, and the buttons control that song. The sound stays on the phone or speaker that is already playing it. The Car Thing does not play audio itself. You can also follow a Home Assistant media player when Spotify is idle.

[![Open your Home Assistant instance and show the add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fumousesonic%2Fbridgething-homeassistant)

That button opens your Home Assistant and fills in this GitHub repository: `https://github.com/umousesonic/bridgething-homeassistant`.

## What you need

- Home Assistant OS, or Home Assistant Supervised, with the **Apps** page. A Home Assistant Core install without the Supervisor cannot install this app.
- A 64-bit system. This app is built for Home Assistant Green, a 64-bit Raspberry Pi, and a normal 64-bit PC. It is not built for 32-bit Home Assistant.
- A Car Thing already flashed with Bridgething.
- A USB cable from the Car Thing to the Home Assistant computer. A cable into a different computer will not work.
- A media player that is already in Home Assistant. Spotify, Music Assistant, or any other `media_player` is fine.

## Install

1. Click the **Add repository** button at the top of this page. Home Assistant opens and asks to add the repository. Confirm it.
2. Go to **Settings → Apps**. Find **Bridgething** and open it.
3. Install it. Home Assistant asks for extra access so the app can see USB devices and set up the USB network. Allow that access.
4. Start the app and leave it running. On the **Log** tab you should see `Installed the Bridgething integration` and `api listening on 8770`.
5. Restart Home Assistant: **Settings → System → Restart**. The app copies its integration into Home Assistant on startup, and Home Assistant loads that integration only after a restart.
6. Go to **Settings → Devices & services → Add integration**, search for **Bridgething**, and add it.

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=bridgething)

7. Keep the address as `http://172.30.32.1:8770`. That is how Home Assistant reaches the app. Pick the media player that actually plays the sound.
8. On the Spotify step, choose **Log in to Spotify**. Click **Open the Spotify login page**. Approve access. The browser then goes to `http://127.0.0.1:8888/login` and that page fails to load. Copy the full address from the browser bar into **Address from the browser bar**. The companion stores the login. The Car Thing does not receive it. If you logged in with an older version, log in again. That older login cannot see what Spotify is playing.
9. Plug in the Car Thing. After a short wait, **Link** on the Bridgething device turns on. **Spotify account** shows the Spotify user id once the companion has finished signing in.

To log in later, open the Bridgething integration and choose **Reconfigure**.

You only add the integration once. After that, the device stays under **Settings → Devices & services → Bridgething**.

## What you can control

Open the Bridgething device. These controls are on the main page:

| Control | What it does |
| --- | --- |
| **Link** | On when the USB link and the connection to the Car Thing are both up. |
| **Spotify account** | The Spotify account this companion is logged in as. |
| **Set backlight** | The brightness you set, from 0 to 100. Moving it switches the panel to manual mode. |
| **Backlight** | The brightness the screen is using now. Auto mode changes this as the room light changes. |
| **Backlight mode** | **Auto** follows the room light. **Manual** keeps the set backlight value. |
| **Ambient light** | The light level the Car Thing sees. |
| **Current app** | The screen that is open. |
| **Webapp** | Switch the Car Thing to another screen. |
| **Now playing** | The song Spotify is playing, with its cover. Play, pause, next, previous, seek, shuffle, and repeat control that Spotify player. When Spotify is idle, these follow the media player you picked. |
| **Reboot** | Restarts the Car Thing. |
| **Power off** | Turns the Car Thing off. |
| **Nickname** | The name shown for the device. |
| **M button gesture** | What a press of the M button does: five presses, or a long press. |
| **Paired Bluetooth device** | Pick a paired device, then use **Connect Bluetooth** or **Forget Bluetooth**. |
| **Bluetooth discoverable** | Lets a phone find the Car Thing. |
| **Bluetooth alias** | The Bluetooth name other devices see. |
| **Browser URL** and **Open URL** | Opens a web page on the Car Thing. |
| **Open companion on reconnect** | Opens the companion screen again after the USB link comes back. |

**Factory reset** is in the configuration section of the device page. It erases the Car Thing. Use it only when you mean to wipe the device.

Extra readings, such as temperature, memory, disk space, and the serial number, are diagnostic. They stay hidden until you enable them on the device page.

## Day to day

Start the Bridgething app, then plug in the Car Thing. **Link** turns on when the device is ready. Start a song in Spotify on your phone or another speaker. The Car Thing shows that song and its cover, and its buttons pause, skip, and seek on that same Spotify player.

When nothing is playing in Spotify, the screen follows the Home Assistant media player you picked, and the buttons control that player.

To follow a different player later, open the Bridgething integration and change the media player in its options.

## If Link stays off

- The USB cable is plugged into the computer running Home Assistant, and the Bridgething app is started.
- The integration address is `http://172.30.32.1:8770`.
- The app log contains `api listening on 8770`. If the log says the address is already in use, restart the app once.
- After the first install, Home Assistant was restarted so the integration could load.

If the app is running and **Link** is off, unplug the Car Thing, wait a few seconds, and plug it back in.

## Update

When this repository publishes a new version, open **Settings → Apps → Bridgething** and install the update. Start the app if it stopped, then restart Home Assistant so the controls match the new version.

## Install by copying the folder

Use this when you cannot add the GitHub repository, for example on a Home Assistant that has no internet.

1. Install the **Samba share** app from **Settings → Apps**. Turn on the `local_apps` share, set a username and password, and start it.
2. On your computer, open `\\<home-assistant-ip>\local_apps`. Copy the `bridgething` folder from this repository into that share, so the file `config.yaml` is at `local_apps\bridgething\config.yaml`.
3. In Home Assistant, open **Settings → Apps**, open the menu at the top right, and choose **Check for updates**.
4. Under **Local apps**, install **Bridgething**, allow the extra access, and start it.
5. Restart Home Assistant, then add the Bridgething integration as in the steps above. The address is still `http://172.30.32.1:8770`.

After you change the files in `local_apps`, check for updates again and install the update. Home Assistant rebuilds the app from those files.
