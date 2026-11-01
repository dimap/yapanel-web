#!/bin/sh
# Будильник веб-панели. Работает на самой колонке: компьютер с панелью для него не нужен, интернет — тоже.
# Интернет нужен только Алисе (музыка, утреннее шоу): если его нет или Алиса за полминуты так ничего и
# не включила, играет выбранный звук.
#
# alarms.conf, по строке на будильник (пишет панель):
#   id|вкл|ЧЧ:ММ|дни|режим|запрос|файл звука|название звука|громкость 1–10|нарастание, с|выкл. через, мин|отложить, мин|подпись
# дни — цифры 1–7 (пн–вс), пусто — один раз; режим: sound | wave | show | music | text.
#
# scenes.conf — сценарии (пишет панель). Сценарий — граф нод: строка S на сценарий, строки N — ноды:
#   S|id|вкл|повод|ЧЧ:ММ|дни|первая нода|название      повод: time — по времени, boot — после включения,
#                                                       manual — только вручную (панель, нода «Другой сценарий»),
#                                                       voice — по команде ассистенту (вместо времени — фразы
#                                                       через запятую; ищет и запускает их сам ассистент)
#   N|id|нода|вид|арг1|арг2|дальше|иначе|x|y            x, y — место ноды в редакторе, демону не нужны
# Виды: query/say/ask текст, vol 0–10 нарастание_с, sound файл, ring RRGGBB секунд, wait секунд,
# wait_until ЧЧ:ММ, wait_quiet до_секунд, stop, scene id, anim ring:имя|clock:имя секунд, assistant on|off,
# if проверка аргумент, repeat раз, note (не выполняется). Анимацию часов панель кладёт готовым скриптом anim_<имя>.sh.
# У if и repeat два выхода: «дальше» — да / ещё круг, «иначе» — нет / круги кончились.
# Тексты — только данные: ноду выполняет case ниже, через eval/sh -c они не проходят.

D=/data/quasar/webpanel/alarm
CONF=$D/alarms.conf
LAST=$D/last_fired
SNOOZE=$D/snooze
LOG=$D/alarm.log
STATE=/tmp/webpanel_alarm.state
CMD=/tmp/webpanel_alarm.cmd
FIRE=/tmp/webpanel_alarm.fire
SCENES=$D/scenes.conf
SFIRE=/tmp/webpanel_scene.fire       # «запустить сейчас» из панели: id сценария
SRUN=/tmp/webpanel_scene.            # SRUN<id> — pid идущего сценария (панель останавливает по нему)
MLOG=/data/quasar/daemons_logs/maind.log
Q=/system/vendor/quasar
[ -f $D/localtime ] && export TZ=:$D/localtime
RING_LEDS="red:ring1 green:ring1 blue:ring1 red:ring2 green:ring2 blue:ring2 red:ring3 green:ring3 blue:ring3"

log() {
    echo "$(date '+%d.%m %H:%M:%S') $*" >> $LOG
    [ "$(wc -c < $LOG)" -gt 30000 ] && tail -n 150 $LOG > $LOG.t && mv $LOG.t $LOG
}

# Громкость Алисы 0–10 → значение ALSA Master (как platformToAlsaMap у Quasar: 0→0, 1→49, 20→255).
alsa_of() { p=$(($1 * 2)); [ $p -le 0 ] && echo 0 || echo $((49 + (p - 1) * 206 / 19)); }
get_alsa() { amixer get Master | sed -n 's/.*Mono: Playback \([0-9][0-9]*\).*/\1/p; s/.*Front Left: Playback \([0-9][0-9]*\).*/\1/p' | head -n 1; }
set_alsa() { amixer -q set Master "$1" 2>/dev/null; }

playing() { grep -q "state: RUNNING" /proc/asound/card*/pcm*p/sub*/status 2>/dev/null; }

online() {
    ping -c 1 -W 3 77.88.8.8 >/dev/null 2>&1 || ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1 ||
        wget -q -T 5 -O /dev/null http://ya.ru 2>/dev/null
}

# Текстовые запросы Алисе — через отладочный CLI maind: команда «q», текст — следующей строкой.
alice() {
    (
        sleep 2
        for t in "$@"; do printf 'q\n'; sleep 0.5; printf '%s\n' "$t"; sleep 2.5; done
        printf 'x\n'; sleep 1
    ) | LD_LIBRARY_PATH=$Q/libs $Q/maind --cli >/dev/null 2>&1 &
    ap=$!
    i=0; while kill -0 $ap 2>/dev/null && [ $i -lt 40 ]; do sleep 1; i=$((i + 1)); done
    kill $ap 2>/dev/null
}

# Кольцо — через framebuffer, как у самого Quasar; после будильника возвращаем его Quasar.
# Только если /dev/fb0 — кольцо Лайт 2 (3 пикселя): на других моделях там может быть экран.
FB_RING=; [ "$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)" = "3,1" ] && FB_RING=1
ring() { [ -n "$FB_RING" ] && echo -e -n "$1$1$1" > /dev/fb0 2>/dev/null; }
ring_release() {
    for n in $RING_LEDS; do
        t=/sys/class/leds/plum:$n/trigger
        grep -q '\[led_ring_fb\]' $t || echo led_ring_fb > $t
    done
    ring '\x00\x00\x00'
}

# Ждём, что Алиса действительно заиграла: музыка — звук держится 6 с подряд, свой запрос — хоть какой-то ответ.
wait_alice() {
    need=6; [ "$1" = text ] && need=1
    i=0; run=0
    while [ $i -lt 30 ]; do
        [ -f $CMD ] && return 0
        if playing; then run=$((run + 1)); [ $run -ge $need ] && return 0; else run=0; fi
        sleep 1; i=$((i + 1))
    done
    return 1
}

fire() {
    id=$1 mode=$2 query=$3 sound=$4 vol=$5 ramp=$6 dur=$7 snz=$8
    rm -f $CMD
    orig=$(get_alsa)
    target=$(alsa_of $vol)
    start=$target; [ "$ramp" -gt 0 ] && start=$(alsa_of 1)
    via=sound
    echo "ringing|$id|$(date +%s)|starting" > $STATE
    if [ "$mode" != sound ]; then
        case $mode in
            wave) q="включи мою волну" ;;
            show) q="включи утреннее шоу" ;;
            music) q="включи $query" ;;
            *) q=$query ;;
        esac
        if ! online; then
            log "$id: нет интернета — играет свой звук"
        else
            v=$vol; [ "$ramp" -gt 0 ] && v=1
            alice "громкость $v" "$q"
            if wait_alice $mode; then via=alice; log "$id: Алиса — «$q»"
            else log "$id: Алиса не включила «$q» — играет свой звук"; fi
        fi
    fi
    echo "ringing|$id|$(date +%s)|$via" > $STATE
    apid=
    if [ $via = sound ]; then
        f=$D/$sound; [ -f "$f" ] || f=$D/default.wav
        log "$id: звук $(basename "$f")"
        set_alsa $start
        (while [ ! -f $CMD ]; do aplay -q "$f" 2>/dev/null || sleep 1; done) &
        apid=$!
    fi
    # Кнопка Алисы или голос («Алиса, …») выключают будильник. Следим за новыми строками лога maind.
    off=$(wc -c < $MLOG 2>/dev/null || echo 0)
    t=0 quiet=0 synced=0 why=
    while :; do
        [ -f $CMD ] && { why=$(cat $CMD); break; }
        size=$(wc -c < $MLOG 2>/dev/null || echo 0)
        [ "$size" -lt "$off" ] && off=0
        if [ "$size" -gt "$off" ]; then
            if tail -c +$((off + 1)) $MLOG | head -c $((size - off)) | grep -q -E "Button click: activate|VOICEPRINT_MATCH"; then
                why=button; break
            fi
            off=$size
        fi
        if [ "$ramp" -gt 0 ] && [ $t -le "$ramp" ]; then
            set_alsa $((start + (target - start) * t / ramp))
        elif [ $via = alice ] && [ $synced = 0 ]; then
            synced=1; [ "$ramp" -gt 0 ] && alice "громкость $vol"
        fi
        if [ $via = alice ]; then
            # Музыку выключили голосом или кнопкой — будильник тоже закончился.
            if playing; then quiet=0; else quiet=$((quiet + 1)); [ $quiet -ge 8 ] && { why=ended; break; }; fi
        else
            [ $((t % 2)) = 0 ] && ring '\xff\x47\x6c' || ring '\x40\x12\x1b'
        fi
        [ "$dur" -gt 0 ] && [ $t -ge $((dur * 60)) ] && { why=timeout; break; }
        sleep 1; t=$((t + 1))
    done
    [ -z "$why" ] && why=stop
    touch $CMD
    if [ -n "$apid" ]; then
        kill $apid 2>/dev/null; killall aplay 2>/dev/null; sleep 0.3; killall aplay 2>/dev/null
        set_alsa $orig
        ring_release
    elif [ "$why" != ended ]; then
        alice "стоп"
    fi
    if [ "$why" = snooze ] && [ "$snz" -gt 0 ]; then
        echo "$(($(date +%s) + snz * 60))|$id" > $SNOOZE
        log "$id: отложен на $snz мин"
    else
        log "$id: выключен ($why)"
    fi
    rm -f $CMD
    echo "idle" > $STATE
}

# ---------- сценарии ----------
SLOG=$D/scenes.log
slog() {
    echo "$(date '+%d.%m %H:%M:%S') $*" >> $SLOG
    [ "$(wc -c < $SLOG)" -gt 30000 ] && tail -n 150 $SLOG > $SLOG.t && mv $SLOG.t $SLOG
    return 0   # иначе «… && slog A || slog B» напишет обе строки: проверка размера выше обычно ложна
}
mins() { h=${1%:*} m=${1#*:}; echo $(( ${h#0} * 60 + ${m#0} )); }

# Спросить своего ассистента (device/assistant) и дождаться, пока он ответит.
ask() {
    [ -f /tmp/webpanel_assistant.json ] || { slog "ассистент не запущен — «$1» пропущен"; return; }
    printf '{"ask":"%s"}\n' "$1" > /tmp/webpanel_assistant.cmd.new && mv /tmp/webpanel_assistant.cmd.new /tmp/webpanel_assistant.cmd
    sleep 3
    i=0; while ! grep -q '"state":"idle"' /tmp/webpanel_assistant.json && [ $i -lt 180 ]; do sleep 1; i=$((i + 1)); done
}

check() {
    case $1 in
        time)   # «22:00-07:00»: через полночь тоже
            now=$(mins "$(date +%H:%M)") f=$(mins "${2%-*}") to=$(mins "${2#*-}")
            if [ $f -le $to ]; then [ $now -ge $f ] && [ $now -lt $to ]; else [ $now -ge $f ] || [ $now -lt $to ]; fi ;;
        days) case "$2" in *"$(date +%u)"*) return 0 ;; esac; return 1 ;;
        playing) playing ;;
        online) online ;;
        vol_above) [ "$(get_alsa)" -gt "$(alsa_of "$2")" ] ;;
        chance) [ $(($(od -An -N2 -tu2 /dev/urandom) % 100)) -lt "$2" ] ;;
        assistant_mode) grep -q '"mode":true' /tmp/webpanel_assistant.json 2>/dev/null ;;
        *) return 1 ;;
    esac
}

# Нода: 0 — идти по «дальше», иначе — по «иначе».
node() {
    kind=$1 a=$2 b=$3 sid=$4 nid=$5
    case $kind in
        query) alice "$a" ;;
        say) alice "Повтори за мной: $a" ;;
        ask) ask "$a" ;;
        assistant)   # режим ассистента: все вопросы к Алисе уходят своей модели
            [ -f /tmp/webpanel_assistant.json ] || { slog "ассистент не запущен — режим не переключён"; return 0; }
            m=false; [ "$a" = on ] && m=true
            printf '{"mode":%s}\n' $m > /tmp/webpanel_assistant.cmd.new && mv /tmp/webpanel_assistant.cmd.new /tmp/webpanel_assistant.cmd ;;
        vol)
            # плавно — через микшер, а в конце говорим Алисе ту же громкость, чтобы она знала о ней
            if [ "${b:-0}" -gt 0 ]; then
                from=$(get_alsa) to=$(alsa_of "$a") t=0
                while [ $t -lt "$b" ]; do set_alsa $((from + (to - from) * t / b)); sleep 1; t=$((t + 1)); done
            fi
            alice "громкость $a" ;;
        sound) [ -f "$D/$a" ] && aplay -q "$D/$a" 2>/dev/null ;;
        ring)
            c=$a; r=${c%????} g=${c#??}; g=${g%??}; bl=${c#????}
            ring "\\x$bl\\x$g\\x$r"; sleep "${b:-5}"; ring_release ;;
        wait) sleep "${a:-1}" ;;
        wait_until)
            s=$(( $(mins "$a") * 60 - $(mins "$(date +%H:%M)") * 60 - $(date +%S | sed 's/^0//') ))
            [ $s -le 0 ] && s=$((s + 86400)); sleep $s ;;
        wait_quiet) i=0; while playing && [ $i -lt "${a:-600}" ]; do sleep 1; i=$((i + 1)); done ;;
        stop) alice "стоп" ;;
        scene) ( run_scene "$a" ) ;;
        anim)
            # предустановленная анимация на «секунд» b: кольцо — штатным led_util, часы — скриптом от панели
            ap=   # ap пишет и alice() — не убить бы чужой pid
            # своя анимация — «my/имя»: лежит в webpanel/anims, а не в Quasar; скрипт часов — с «~» вместо «/»
            case $a in
                ring:my/*) LD_LIBRARY_PATH=$Q/libs $Q/led_util "/data/quasar/webpanel/anims/ring/${a#ring:my/}" >/dev/null 2>&1 & ap=$! ;;
                ring:*) LD_LIBRARY_PATH=$Q/libs $Q/led_util "$Q/ledpatterns/oknix/${a#ring:}" >/dev/null 2>&1 & ap=$! ;;
                clock:*) f="$D/anim_$(echo "${a#clock:}" | tr / '~').sh"; [ -f "$f" ] && { sh "$f" & ap=$!; } ;;
            esac
            if [ -n "$ap" ]; then sleep "${b:-5}"; kill $ap 2>/dev/null; ap=; fi
            case $a in ring:*) ring_release ;; esac ;;
        if) check "$a" "$b"; return ;;
        repeat)
            f=$SRUN$sid.c$nid; rc=$(cat $f 2>/dev/null || echo 0)
            [ "$rc" -lt "$a" ] && { echo $((rc + 1)) > $f; return 0; }
            rm -f $f; return 1 ;;
    esac
    return 0
}

run_scene() {
    # Своя группа позволяет остановить сценарий вместе с дочерними командами.
    setsid sh "$0" --scene "$1" >/dev/null 2>&1 &
}

run_scene_worker() {
    id=$1
    line=$(grep "^S|$id|" $SCENES 2>/dev/null | head -n 1)
    [ -n "$line" ] || return
    IFS='|' read -r _ _ _ _ _ _ start name <<EOF
$line
EOF
    IFS= read -r selfstat < /proc/self/stat
    printf '%s\n' "$selfstat" | sed 's/^.*) //' | awk '{print $20}' > $SRUN$id.start
    echo "${selfstat%% *}" > $SRUN$id
    trap 'rm -f $SRUN$id $SRUN$id.start $SRUN$id.nodes $SRUN$id.c* $SRUN$id.v*' 0
    trap 'exit 1' HUP INT TERM
    grep "^N|$id|" $SCENES > $SRUN$id.nodes
        slog "«$name»: начат"
        # Защита от зацикливания: каждая нода — не больше 20 раз за запуск (у «Повтора» свой счёт, до 20 кругов).
        # Счёт — в файлах: переменные с номером ноды в имени потребовали бы eval.
        pc=$start
        while [ -n "$pc" ]; do
            l=$(grep "^N|$id|$pc|" $SRUN$id.nodes | head -n 1)
            [ -n "$l" ] || break
            IFS='|' read -r _ _ _ kind a b nx el _ <<EOF
$l
EOF
            if [ "$kind" != repeat ]; then
                v=$(cat $SRUN$id.v$pc 2>/dev/null || echo 0)
                if [ "$v" -ge 20 ]; then slog "«$name»: остановлен — нода $pc прошла 20 раз, похоже, цикл без выхода"; break; fi
                echo $((v + 1)) > $SRUN$id.v$pc
            fi
            if node "$kind" "$a" "$b" "$id" "$pc" < /dev/null; then pc=$nx; else pc=$el; fi
        done
        slog "«$name»: готово"
} < /dev/null

check_scenes() {
    [ -f $SCENES ] || return
    due=
    while IFS='|' read -r t id on trig tm days rest; do
        [ "$t" = S ] && [ "$on" = 1 ] && [ "$trig" = time ] && [ "$tm" = "$now" ] || continue
        case "$days" in "" | *"$dow"*) ;; *) continue ;; esac
        grep -q "^s$id $stamp\$" $LAST 2>/dev/null && continue
        echo "s$id $stamp" >> $LAST
        [ -z "$days" ] && sed -i "s/^S|$id|1|/S|$id|0|/" $SCENES
        due="$due $id"
    done < $SCENES
    for id in $due; do run_scene "$id"; done
}

# После включения колонки: демон стартует из хука загрузки раньше Алисы — ждём её и ещё минуту.
# Только при свежей загрузке: панель перезапускает демон при каждом сохранении.
boot_scenes() {
    [ -f $SCENES ] && grep -q "^S|[^|]*|1|boot|" $SCENES || return
    up=$(cut -d. -f1 /proc/uptime); [ "$up" -lt 600 ] || return
    [ -f /tmp/webpanel_scene.boot ] && return
    touch /tmp/webpanel_scene.boot
    (
        i=0; while ! pidof maind >/dev/null && [ $i -lt 300 ]; do sleep 2; i=$((i + 2)); done
        sleep 60
        for id in $(grep "^S|[^|]*|1|boot|" $SCENES | cut -d'|' -f2); do run_scene "$id"; done
    ) < /dev/null &
}

# Строка конфига → fire
run_id() {
    line=$(grep "^$1|" $CONF 2>/dev/null | head -n 1)
    [ -n "$line" ] || return
    IFS='|' read -r id on tm days mode query sound sname vol ramp dur snz label <<EOF
$line
EOF
    fire "$id" "$mode" "$query" "$sound" "${vol:-5}" "${ramp:-0}" "${dur:-0}" "${snz:-9}" < /dev/null
}

check_minute() {
    now=$(date +%H:%M) dow=$(date +%u) stamp=$(date +%Y%m%d%H%M) ep=$(date +%s)
    if [ -f $SNOOZE ]; then
        IFS='|' read -r sat sid < $SNOOZE
        if [ "$ep" -ge "${sat:-0}" ]; then rm -f $SNOOZE; run_id "$sid"; fi
    fi
    check_scenes
    [ -f $CONF ] || return
    due=
    while IFS='|' read -r id on tm days mode rest; do
        [ "$on" = 1 ] && [ "$tm" = "$now" ] || continue
        case "$days" in "" | *"$dow"*) ;; *) continue ;; esac
        grep -q "^$id $stamp\$" $LAST 2>/dev/null && continue
        echo "$id $stamp" >> $LAST
        [ -z "$days" ] && sed -i "s/^$id|1|/$id|0|/" $CONF
        due="$due $id"
    done < $CONF
    [ -f $LAST ] && tail -n 50 $LAST > $LAST.t && mv $LAST.t $LAST
    for id in $due; do run_id "$id"; done
}

if [ "$1" = --scene ]; then run_scene_worker "$2"; exit $?; fi

echo idle > $STATE
log "запущен, время на колонке $(date '+%H:%M %Z')"
boot_scenes
while :; do
    check_minute
    # До начала следующей минуты; раз в секунду проверяем «проверить сейчас» из панели.
    s=$(date +%S); s=${s#0}
    i=$((60 - s))
    while [ $i -gt 0 ]; do
        # Срок повтора проверяем каждую секунду, не дожидаясь смены минуты.
        if [ -f $SNOOZE ]; then
            IFS='|' read -r sat sid < $SNOOZE
            [ "$(date +%s)" -ge "${sat:-0}" ] && break
        fi
        if [ -f $FIRE ]; then id=$(cat $FIRE); rm -f $FIRE; log "$id: проверка из панели"; run_id "$id"; break; fi
        # «id» — кнопка в панели, «id voice» — команда ассистенту
        if [ -f $SFIRE ]; then
            read -r id src < $SFIRE; rm -f $SFIRE
            if [ "$src" = voice ]; then slog "запуск по команде ассистенту"; else slog "запуск из панели"; fi
            run_scene "$id"
        fi
        sleep 1; i=$((i - 1))
    done
done
