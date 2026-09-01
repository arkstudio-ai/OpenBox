# video_thumbnail (vendored)

Upstream: https://github.com/justsoft/video_thumbnail — pub `video_thumbnail`
0.5.6, the newest release (2025-05-14) and the last one there has been.

Vendored for one reason: the published `android/build.gradle` pins AGP 4.1.0
and resolves through `jcenter()`. jcenter is gone, and that AGP cannot load
beside this app's AGP 9 / Gradle 9 — Android would not configure at all.

Only `android/build.gradle` is rewritten. `lib/`, the Java plugin and the
ObjC/podspec side are the published files, unmodified, so behaviour is
identical on both platforms.

The alternative was swapping in a maintained package, but the maintained ones
thumbnail a **local file**. This one hands the URL straight to the platform
decoder, which fetches the moov atom and one frame instead of the whole video —
and every video here is behind a presigned URL. That is a property worth
keeping a fork for.

If upstream ever ships a build script that works with a current AGP, delete
this directory and put `video_thumbnail:` back in pubspec.yaml.
