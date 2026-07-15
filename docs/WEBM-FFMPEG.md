**Memo: WebM enablement and FFmpeg rebuild notes**

What was done:

* WebM was enabled at the app level by allowing `.webm` in the Flask upload validation and in the browser file input / drag-drop checks, so WebM files could be selected and submitted like MP4/MOV/M4V.
* The real blocker was not the Flask code. The failing files were **WebM with AV1 video**, and the custom FFmpeg build had subtitle support (`libass`) but did **not** have the `libdav1d` AV1 software decoder enabled. FFmpeg’s subtitle filters require `--enable-libass`, and AV1 decoding via dav1d requires installing dav1d and configuring FFmpeg with `--enable-libdav1d`. ([FFmpeg][1])
* After rebuilding the same custom binary at `~/ffmpeg-full/bin/ffmpeg` with `libdav1d`, WebM/AV1 input started working without needing a `render_preview()` rewrite.

How to verify in the future:

* Check subtitle support:

  * `"$HOME/ffmpeg-full/bin/ffmpeg" -hide_banner -filters | grep subtitles`
* Check AV1 decoder support:

  * `"$HOME/ffmpeg-full/bin/ffmpeg" -hide_banner -decoders | grep -E '(^ V.*av1|^ V.*libdav1d)'`
* Desired result:

  * subtitle filter available because of `--enable-libass`
  * `libdav1d` present in decoders for reliable AV1 software decode ([FFmpeg][1])

How to rebuild FFmpeg for future use:

1. Install/build **dav1d** first.
2. Reconfigure FFmpeg with the needed external libraries, especially:

   * `--enable-libass`
   * `--enable-libdav1d`
   * plus your existing codec flags such as `--enable-libx264 --enable-libx265 --enable-libvpx --enable-gpl`
3. Rebuild and reinstall to your custom prefix, for example `~/ffmpeg-full`. FFmpeg’s docs explicitly say to install the dav1d library first, then pass `--enable-libdav1d` at configure time. ([FFmpeg][2])

Minimal configure shape:

```bash
./configure \
  --prefix="$HOME/ffmpeg-full" \
  --enable-gpl \
  --enable-libass \
  --enable-libx264 \
  --enable-libx265 \
  --enable-libvpx \
  --enable-libdav1d
```

Bottom line:

* **App change:** allow `.webm`
* **Build change:** FFmpeg must include **both** `libass` and `libdav1d`
* **Reason:** WebM was accepted by the app, but AV1-in-WebM only became usable after the FFmpeg rebuild.

[1]: https://ffmpeg.org/ffmpeg-filters.html?utm_source=chatgpt.com "FFmpeg Filters Documentation"
[2]: https://www.ffmpeg.org/general.html?utm_source=chatgpt.com "General Documentation"
