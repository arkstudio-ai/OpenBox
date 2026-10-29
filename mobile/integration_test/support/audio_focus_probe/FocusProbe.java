package com.bossip.qa.focusprobe;
import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.media.AudioManager;
import android.media.AudioFocusRequest;
import android.media.AudioAttributes;
import android.util.Log;
public class FocusProbe extends Activity {
  AudioManager manager;
  AudioFocusRequest request;
  @Override public void onCreate(Bundle b) {
    super.onCreate(b);
    manager = (AudioManager)getSystemService(AUDIO_SERVICE);
    int kind=getIntent().getIntExtra("kind",3);
    int millis=getIntent().getIntExtra("millis",3000);
    request=new AudioFocusRequest.Builder(kind)
      .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
      .setOnAudioFocusChangeListener(change -> Log.i("FocusProbe", "focus="+change)).build();
    int result=manager.requestAudioFocus(request);
    Log.i("FocusProbe", "requested="+kind+" result="+result);
    new Handler(Looper.getMainLooper()).postDelayed(() -> {manager.abandonAudioFocusRequest(request); Log.i("FocusProbe", "released="+kind); finish();},millis);
  }
}
