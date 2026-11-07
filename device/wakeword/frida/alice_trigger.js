var BASE = '/data/data/com.yandex.io.sdk/files';
var TRIGGER = BASE + '/alice_trigger';
var ALIVE = BASE + '/alice_frida_alive';
var inst = null;

function findInstance() {
  Java.perform(function () {
    Java.choose('ru.yandex.io.sdk.jni.YandexIOSDKImpl', {
      onMatch: function (i) {
        if (!inst) inst = i;
      },
      onComplete: function () {
        if (inst) console.log('trigger daemon: instance=', inst);
      }
    });
  });
}

function beat() {
  try {
    var f = new File(ALIVE, 'w');
    f.write('' + Date.now());
    f.flush();
    f.close();
  } catch (e) {}
}

findInstance();
beat();
setInterval(beat, 5000);

var last = '0';
setInterval(function () {
  try {
    if (!inst) findInstance();
    var f = new File(TRIGGER, 'r');
    var s = f.readText();
    f.close();
    if (s.indexOf('1') >= 0 && last !== s && inst) {
      last = s;
      inst.startConversation();
      console.log('trigger daemon: startConversation');
      var w = new File(TRIGGER, 'w');
      w.write('0');
      w.flush();
      w.close();
      last = '0';
    }
  } catch (e) {}
}, 200);
