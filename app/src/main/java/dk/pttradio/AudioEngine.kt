package dk.pttradio

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder

/** 16 kHz mono PCM – optagelse ved PTT, afspilning af modtaget lyd. */
class AudioEngine {

    companion object {
        const val RATE = 16000
        const val CHUNK = 640 // 20 ms
    }

    @Volatile private var track: AudioTrack? = null
    @Volatile private var recording = false
    @Volatile private var generation = 0

    fun startPlayback() {
        if (track != null) return
        val min = AudioTrack.getMinBufferSize(RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(RATE)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .build()
            )
            .setBufferSizeInBytes(maxOf(min, RATE * 2 / 4)) // ~250 ms
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.play()
        track = t
    }

    fun play(data: ByteArray) {
        track?.write(data, 0, data.size)
    }

    fun stopPlayback() {
        val t = track ?: return
        track = null
        try { t.pause(); t.flush(); t.release() } catch (_: Exception) {}
    }

    @SuppressLint("MissingPermission")
    fun startRecording(onChunk: (ByteArray) -> Unit): Boolean {
        if (recording) return true
        val min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION, RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(min, CHUNK * 4)
            )
        } catch (e: Exception) {
            return false
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return false
        }
        recording = true
        val myGen = ++generation
        Thread {
            try {
                rec.startRecording()
                val buf = ByteArray(CHUNK)
                while (recording && generation == myGen) {
                    val n = rec.read(buf, 0, CHUNK)
                    if (n < 0) break
                    if (n > 0) onChunk(buf.copyOf(n))
                }
            } catch (_: Exception) {
            } finally {
                try { rec.stop() } catch (_: Exception) {}
                rec.release()
            }
        }.start()
        return true
    }

    fun stopRecording() {
        recording = false
    }
}
