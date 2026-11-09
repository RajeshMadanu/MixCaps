MixCaps 0.3 - captions for DaVinci Resolve
===========================================
English, Telugish (Telugu + English) and Hinglish (Hindi + English) captions,
written in Roman letters and placed on your timeline as styled, animated Text+.
Everything runs on your own PC. Nothing is uploaded.

WHAT YOU NEED
  - Windows 10 or 11
  - DaVinci Resolve 18 to 21.0 (free or Studio). Resolve 21.1 and later are not supported yet.
  - Internet for the first install
  - Disk space: about 3 GB, plus 1 to 6 GB for each Telugu model you choose
  - An NVIDIA GPU makes it much faster. Without one it still works, just slower.

INSTALL
  1. Unzip this folder anywhere (for example Documents\MixCaps).
  2. Double-click install.bat.
     If Windows says "Windows protected your PC", click "More info" and then "Run anyway".
  3. Answer the questions about Telugu models. Medium is a good middle choice.
     If you only make English or Hinglish videos, answer "n" to all three.
  4. Wait until it says "MixCaps is installed". The first install downloads a few GB.
  5. Restart DaVinci Resolve.

USE
  1. Open your project and timeline in Resolve.
  2. Go to Workspace > Scripts > MixCaps. The MixCaps window opens next to Resolve.
  3. Pick the language and press "Generate captions".
  4. Check the captions in the list and fix any words. Click a time to jump there in Resolve.
  5. On the Style tab, pick a preset or make your own.
  6. Press "Add captions to timeline". They go on a new track called "MixCaps".
     Pressing it again replaces the old MixCaps captions.

STYLES AND PRESETS
  - Built-in looks: GameGlitch, Clean, Bold Outline, Yellow Punch, Boxed, Neon,
    Typewriter, and Stacked (lines stack up, the key line in big pink serif).
  - Customise: font, colour, outline, shadow, background box, size and animation.
    Name it and press "Save preset".
  - Stacked: tap the star next to a caption to make it the big emphasis line
    (or to stop it being picked).
  - Share presets: Style tab > Export gives you a .mixcaps.json file.
    Your friend uses Style tab > Import.
  - "Apply look to timeline" restyles captions that are already on the timeline.

FIX WORDS AUTOMATICALLY
  The "Replace" box with "Remember" ticked fixes that word in every future video.
  Your lists are in %LOCALAPPDATA%\MixCaps (custom_words.txt, replacements.txt).

IF SOMETHING GOES WRONG
  - MixCaps window says "Resolve not connected": in Resolve run Workspace > Scripts > MixCaps again.
  - Captions don't show in the viewer: restart Resolve.
  - Logs: %LOCALAPPDATA%\MixCaps\resolve_log.txt and app_log.txt
  - To remove MixCaps: run uninstall.bat.

Credits: uses faster-whisper (MIT), the vasista22 Telugu Whisper models (Apache 2.0),
the Poppins font (SIL OFL 1.1), and a caption template adapted from AutoSubs (MIT).
See NOTICE.txt.
