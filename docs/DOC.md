
It is a browser UI plus Flask backend for creating **animated burned-in captions** on a video. The user loads a video and an `.srt` or `.vtt`, adjusts caption styling and motion in a live helper overlay, then renders either a short preview or the full output.  

What the front end is doing:

* loads a source video and subtitle file
* shows a draggable/resizable helper caption on top of the displayed video
* exposes typography, placement, animation, colors, background, outline, shadow, spacing, rotation, and preset controls through lil-gui
* saves / imports / exports presets
* sends the current control values with the files to the backend for rendering   

What the backend is doing:

* accepts the uploaded video and subtitle file
* reads video resolution
* converts SRT/VTT into animated ASS subtitles
* applies style and per-line/per-word animation settings
* uses FFmpeg to burn the ASS subtitles into a preview or full rendered MP4
* exposes job polling and returns rendered files when ready   

And the specific feature that caused the crash is also clear:

* this app supports an **active word highlight** mode via `active_word_colour` and `active_word_lead_ms`
* those exist in `defaultControls`
* but they were missing from `controls`
* lil-gui crashed when trying to create the color picker for that missing property  

So yes: the app is not just a generic subtitle editor. It is a **caption animation and render tool** for styling subtitles visually, previewing placement on the actual video frame, and exporting a final hard-subbed video.



Yes.

This app can be extended to:

* extract audio from the uploaded video
* run Whisper transcription
* generate `.vtt`
* keep block timings
* keep word timings
* show those blocks in a left-side dock for review/editing

The right structure would be:

**Backend**

* add a transcription endpoint such as `/api/transcribe`
* extract mono audio from the uploaded video with ffmpeg
* run Whisper on that audio
* return:

  * segment text
  * segment start/end
  * per-word start/end
  * generated VTT text

**Frontend**

* add a left dock panel
* show one caption block per segment
* each block displays:

  * segment time range
  * text
  * words with individual timings
* clicking a block seeks the preview video to that time
* clicking a word could set the active reveal point or allow timing edits
* add “Generate VTT” and “Export VTT” buttons

**Important limitation**
Plain OpenAI Whisper usually gives excellent segment timing, but word timing depends on the implementation you use:

* `openai-whisper` can do timestamps, but word-level handling is less convenient
* `whisperx` is usually better if precise word timings are required
* for your app, `whisperx` is the better fit if the left dock must show words accurately

**Recommended output structure**
The backend should return JSON like:

```json
{
  "segments": [
    {
      "start": 0.16,
      "end": 2.79,
      "text": "Your boss is rich. Maybe because of",
      "words": [
        { "word": "Your", "start": 0.16, "end": 0.32 },
        { "word": "boss", "start": 0.32, "end": 0.56 }
      ]
    }
  ],
  "vtt": "WEBVTT\n..."
}
```

**Yes, this matches your app well**
Because your renderer already benefits from:

* cue blocks
* word timings
* VTT-based reveal logic

So Whisper/WhisperX is a natural source format for this app.

One constraint: some previously uploaded files in the session have expired, so exact code patching against your current files would require re-uploading the current `app.py` and `index.html`.
