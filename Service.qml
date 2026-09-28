import QtQuick
import Quickshell
import Quickshell.Io

// The shell keeps this service loaded even when the bar widget is hidden.
Item {
  id: root
  visible: false
  readonly property string script: Qt.resolvedUrl("endurance.py").toString().replace(/^file:\/\//, "")

  Process {
    id: recorder
    command: ["python3", root.script, "tick"]
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim()) console.warn("Endurance recorder:", text.trim())
    }
  }

  Timer {
    interval: 60000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: if (!recorder.running) recorder.running = true
  }
}
