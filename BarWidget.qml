import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "freeholder.endurance"
  ipcTarget: "freeholder.endurance"

  readonly property string script: Qt.resolvedUrl("endurance.py").toString().replace(/^file:\/\//, "")
  property var report: ({ current: {}, sessions: [], median_s: null })
  property string errorMessage: ""
  property int selected: -1
  property int cursor: 0
  readonly property var sessions: report.sessions || []
  readonly property var current: report.current || ({})
  readonly property var detail: selected >= 0 && selected < sessions.length ? sessions[selected] : null
  readonly property real maxDuration: {
    var maximum = 1
    for (var i = 0; i < Math.min(7, sessions.length); i++) {
      var session = sessions[i]
      maximum = Math.max(maximum, session.duration_s || (Date.now()/1000 - session.unplug_ts))
    }
    return maximum
  }
  readonly property var activeSession: sessions.length && sessions[0].end_reason === null ? sessions[0] : null
  readonly property var lastComplete: {
    for (var i = 0; i < sessions.length; i++)
      if (sessions[i].end_reason === "reconnected" && sessions[i].start_observed) return sessions[i]
    return null
  }
  readonly property color ink: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color muted: Color.foreground

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "—"
    var minutes = Math.max(0, Math.round(Number(seconds) / 60))
    return Math.floor(minutes / 60) + "h " + (minutes % 60 < 10 ? "0" : "") + (minutes % 60) + "m"
  }
  function percent(value) {
    return value === null || value === undefined ? "—" : Math.round(value) + "%"
  }
  function stamp(seconds) {
    return seconds ? Qt.formatDateTime(new Date(Number(seconds) * 1000), "ddd d MMM · HH:mm") : "—"
  }
  function label(session) {
    if (!session) return ""
    var d = new Date(Number(session.unplug_ts) * 1000)
    var today = new Date()
    return d.toDateString() === today.toDateString() ? "TODAY" : Qt.formatDate(d, "ddd d MMM").toUpperCase()
  }
  function refresh() {
    if (!reader.running) reader.running = true
  }
  function applyReport(raw) {
    try {
      var next = JSON.parse(raw)
      if (next && next.error) errorMessage = String(next.error)
      else if (next) { report = next; errorMessage = "" }
    } catch (e) { errorMessage = "Could not read recorder output"; console.warn("Endurance: invalid recorder output", e) }
  }
  function open() { refresh(); root.controller.show() }
  function close() { selected = -1; root.controller.hide() }
  function move(delta) {
    if (selected >= 0) return
    cursor = Math.max(0, Math.min(sessions.length - 1, cursor + delta))
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  visible: current.percent !== undefined || sessions.length > 0

  Process {
    id: reader
    command: ["python3", root.script, "snapshot"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyReport(text) }
    stderr: StdioCollector { waitForEnd: true; onStreamFinished: if (text.trim()) console.warn("Endurance:", text.trim()) }
  }
  Timer { interval: 30000; repeat: true; running: true; triggeredOnStart: true; onTriggered: root.refresh() }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰥔"
    slotSize: Style.bar.iconSlot
    active: !!root.activeSession
    activeColor: root.accent
    tooltipText: root.activeSession
      ? "Endurance · on battery for " + root.duration(Date.now()/1000 - root.activeSession.unplug_ts)
      : root.lastComplete
        ? "Endurance · last session " + root.duration(root.lastComplete.duration_s)
        : "Endurance · battery session history"
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: fittedContentWidth(Style.space(360))
    contentHeight: fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.move(dy !== 0 ? dy : dx) }
      onActivateRequested: if (root.selected < 0 && root.sessions.length > 0) root.selected = root.cursor
      onCloseRequested: if (root.selected >= 0) root.selected = -1; else root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(12)

        Row {
          width: parent.width
          spacing: Style.space(8)
          Text { text: root.detail ? "‹" : "󰁹"; color: root.accent; font.pixelSize: Style.space(20) }
          Text { text: root.detail ? root.label(root.detail) : "Endurance"; color: root.ink; font.bold: true; font.pixelSize: Style.space(16) }
        }
        Text {
          visible: !root.detail
          text: root.errorMessage ? "Recorder: " + root.errorMessage : root.current.percent === undefined ? "Waiting for battery data" : root.percent(root.current.percent) + "  ·  " + (root.current.on_battery ? "On battery" : "External power")
          color: root.ink
          font.pixelSize: Style.space(12)
        }
        Text {
          visible: !!root.detail
          text: root.detail ? root.duration(root.detail.duration_s || (Date.now()/1000 - root.detail.unplug_ts)) + "  ·  " + root.percent(root.detail.start_percent) + " → " + root.percent(root.detail.end_percent === null ? root.current.percent : root.detail.end_percent) : ""
          color: root.ink
          font.pixelSize: Style.space(14)
        }
        Text {
          visible: !root.detail
          text: "RECENT BATTERY SESSIONS"
          color: root.muted; opacity: 0.65; font.bold: true; font.pixelSize: Style.space(10)
        }
        Column {
          visible: !root.detail
          width: parent.width
          spacing: Style.space(8)
          Repeater {
            model: root.sessions.slice(0, 7)
            delegate: Item {
              id: sessionRow
              required property var modelData
              required property int index
              width: parent.width
              height: Style.space(39)
              readonly property real sessionDuration: modelData.duration_s || (Date.now()/1000 - modelData.unplug_ts)
              Rectangle { anchors.fill: parent; color: root.accent; opacity: root.cursor === index ? 0.12 : 0; radius: Style.space(5) }
              Text { text: root.label(parent.modelData); color: root.ink; font.bold: true; font.pixelSize: Style.space(11); anchors.left: parent.left; anchors.top: parent.top }
              Text { text: root.duration(parent.sessionDuration) + (parent.modelData.end_reason === null ? " · ongoing" : parent.modelData.end_reason === "reconnected" && parent.modelData.start_observed ? "" : " · partial"); color: root.ink; font.pixelSize: Style.space(11); anchors.right: parent.right; anchors.top: parent.top }
              Rectangle { x: 0; y: Style.space(24); width: parent.width; height: Style.space(5); radius: height/2; color: root.ink; opacity: 0.13 }
              Rectangle {
                id: durationFill
                x: 0; y: Style.space(24)
                width: Math.max(3, parent.width * Math.min(1, parent.sessionDuration / root.maxDuration))
                height: Style.space(5); radius: height/2
                property real pulse: 0
                color: Qt.tint(root.accent, Qt.rgba(1, 1, 1, pulse * 0.6))

                SequentialAnimation on pulse {
                  running: root.opened && root.current.on_battery && sessionRow.index === 0 && sessionRow.modelData.end_reason === null
                  loops: Animation.Infinite
                  NumberAnimation { from: 0; to: 1; duration: 1800; easing.type: Easing.InOutSine }
                  NumberAnimation { from: 1; to: 0; duration: 1800; easing.type: Easing.InOutSine }
                  onRunningChanged: if (!running) durationFill.pulse = 0
                }
              }
              MouseArea { anchors.fill: parent; hoverEnabled: true; onEntered: root.cursor = parent.index; onClicked: root.selected = parent.index }
            }
          }
        }
        Text {
          visible: !root.detail
          text: root.sessions.length === 0 ? "No sessions yet. Endurance starts when battery use is observed." : "7-session median  " + root.duration(root.report.median_s)
          color: root.muted; opacity: 0.72; font.pixelSize: Style.space(11)
          wrapMode: Text.WordWrap; width: parent.width
        }
        Column {
          visible: !!root.detail
          width: parent.width
          spacing: Style.space(8)
          Text { text: root.detail ? root.stamp(root.detail.unplug_ts) + " observed on battery" : ""; color: root.ink; font.pixelSize: Style.space(11) }
          Text { text: root.detail ? (root.detail.reconnect_ts ? root.stamp(root.detail.reconnect_ts) + (root.detail.end_reason === "reconnected_after_sleep" ? " AC observed after sleep" : root.detail.end_reason === "reconnected" ? " AC observed" : " recording ended") : "In progress") : ""; color: root.ink; font.pixelSize: Style.space(11) }
          Text { text: root.detail && root.detail.start_observed ? "Unplug detected" : "Start first observed; unplug time unknown"; color: root.muted; opacity: 0.7; font.pixelSize: Style.space(10) }
          Text { text: root.detail ? "Awake " + root.duration(Math.max(0, (root.detail.duration_s || Date.now()/1000 - root.detail.unplug_ts) - root.detail.suspend_s)) + "  ·  asleep " + root.duration(root.detail.suspend_s) : ""; color: root.ink; font.pixelSize: Style.space(11) }
          Text { text: root.detail && root.detail.consumed_wh !== null ? "Energy used  " + root.detail.consumed_wh.toFixed(1) + " Wh " + (root.detail.energy_kind === "measured" ? "measured" : "approx.") : "Energy used  —"; color: root.ink; font.pixelSize: Style.space(11) }
          Text {
            text: {
              if (!root.detail || root.detail.end_reason !== "reconnected" || !root.detail.start_observed || !root.detail.duration_s || root.detail.start_percent === null || root.detail.end_percent === null || root.detail.start_percent <= root.detail.end_percent) return "Equivalent 100→0  —"
              var fraction = (root.detail.start_percent - root.detail.end_percent) / 100
              return "Equivalent 100→0  " + root.duration(root.detail.duration_s / fraction) + " · normalised"
            }
            color: root.muted; font.pixelSize: Style.space(11)
          }
          Text { text: "DISCHARGE CURVE"; color: root.muted; opacity: 0.65; font.bold: true; font.pixelSize: Style.space(10) }
          Item {
            width: parent.width; height: Style.space(60)
            Rectangle { anchors.fill: parent; color: root.ink; opacity: 0.06; radius: Style.space(5) }
            Repeater {
              model: root.detail ? root.detail.samples : []
              delegate: Rectangle {
                required property var modelData
                required property int index
                readonly property int count: root.detail ? root.detail.samples.length : 0
                width: Math.max(2, parent.width / Math.max(1, count) - 2)
                height: parent.height * Math.max(0.02, Math.min(1, modelData.percent / 100))
                x: index * parent.width / Math.max(1, count)
                anchors.bottom: parent.bottom
                color: root.accent
                opacity: 0.85
              }
            }
          }
          Text { text: "CPU ACTIVITY PROXY · EXPERIMENTAL"; color: root.muted; opacity: 0.65; font.bold: true; font.pixelSize: Style.space(10) }
          Repeater {
            model: root.detail ? root.detail.activity.slice(0, 5) : []
            delegate: Row {
              required property var modelData
              width: parent.width
              Text { text: modelData.name; color: root.ink; width: parent.width * 0.7; elide: Text.ElideRight; font.pixelSize: Style.space(11) }
              Text { text: (modelData.cpu_ticks / Math.max(1, root.detail.cpu_total) * 100).toFixed(0) + "% CPU time"; color: root.muted; font.pixelSize: Style.space(10) }
            }
          }
          Text { visible: root.detail && root.detail.activity.length === 0; text: "No process activity recorded yet"; color: root.muted; font.pixelSize: Style.space(11) }
          Text { text: "Click or press Esc to return"; color: root.muted; opacity: 0.55; font.pixelSize: Style.space(10) }
          MouseArea { width: parent.width; height: Style.space(16); onClicked: root.selected = -1 }
        }
        Text {
          text: "by Freeholder"
          width: parent.width
          horizontalAlignment: Text.AlignRight
          color: root.muted
          opacity: 0.4
          font.pixelSize: Style.space(10)
        }
      }
    }
  }
}
