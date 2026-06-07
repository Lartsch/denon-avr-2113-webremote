# denon-avr-2113-webremote
A Python-based tool that communicates with a Denon AVR 2113 over Telnet, provides quite a comprehensive Web UI for remote control and also supports Google Home / Alexa integration via Sinric Pro (free plan sufficient)

## Setup
- Make sure network control is enabled for standby mode on your Denon AVR 2113
- Adjust the variables at the top of the script
- Make sure you install the requirements: `python3 -m pip install --upgrade sinricpro aiohttp asyncio`
- Run the script with python3: `python3 server.py`

## General Notes
- This was developed for Windows but should work for Linux too. Have not tested yet. Slight adjustments might be needed.
- This is customized to my needs - feel free to build on it to make it suitable for you
- This was created based on the telnet protocol specification for the Denon AVR 2113 which was released around 2012/2013
  - It might be compatible with some other Denon AVRs but I have not tested and do not plan to add more supported AVRs
  - Yet, it does not require much work to adjust / add the Telnet commands for other Denon AVRs
 - I recommend running the script on a network device that will never go to sleep and is connected via LAN

## Notes on Sinric Pro
- Simply create a free Sinric Pro account and add a device based on the "TV" template, then use the shown configuration to adjust the variables in the script - no additional setup needed
- The script supports volume, power state, mute, media control and limited source mode control (adjusted to my usecase) via Sinric Pro or connected Google Home / Alexa instances (so just the basic features)
- You could easily create a custom device template and adjust the script to support more features via Sinric Pro

## The End
- Usually I would go much more into detail about the features, behaviors and implementations, but I just want to share this project in case someone else might enjoy it or make use of it
- Nevertheless, at least here are some screenshots:

<img width="1670" height="1725" alt="screencapture-denon-home-nala-2026-06-07-14_18_39" src="https://github.com/user-attachments/assets/0abc3c24-467d-4705-8ff5-caebd01ce40f" />
<img width="1670" height="1607" alt="screencapture-denon-home-nala-2026-06-07-14_18_51" src="https://github.com/user-attachments/assets/8ecb90b0-7a59-4660-89c7-c4d0eec00d67" />
<img width="1670" height="1057" alt="screencapture-denon-home-nala-2026-06-07-14_19_11" src="https://github.com/user-attachments/assets/636f1db3-3e12-44fd-aaa3-a60c7a9ff2db" />
<img width="1670" height="1057" alt="screencapture-denon-home-nala-2026-06-07-14_19_00" src="https://github.com/user-attachments/assets/ca51334b-7475-4874-9c95-257295e171cf" />
<img width="1672" height="1057" alt="Screenshot 2026-06-07 142026" src="https://github.com/user-attachments/assets/576f0847-3075-41f0-b7cb-9b17b67206bd" />
<img width="480" height="830" alt="Screenshot 2026-06-07 142008" src="https://github.com/user-attachments/assets/6fd58742-a5f6-4811-b8b4-b85a56bd0ca4" />
