/* AetherOS Calamares slideshow */
import QtQuick 2.0
import calamares.slideshow 1.0

Presentation {
    id: presentation

    Timer {
        interval: 4000
        running: presentation.activatedInCalamares
        repeat: true
        onTriggered: presentation.goToNextSlide()
    }

    Slide {
        anchors.fill: parent
        Rectangle {
            anchors.fill: parent
            color: "#0a0b14"
        }
        Column {
            anchors.centerIn: parent
            spacing: 20
            Text { text: "◈"; color: "#818cf8"; font.pixelSize: 72; anchors.horizontalCenter: parent.horizontalCenter }
            Text { text: "AetherOS"; color: "#e2e8f0"; font.pixelSize: 36; font.bold: true; anchors.horizontalCenter: parent.horizontalCenter }
            Text { text: "Your AI-native operating system"; color: "#64748b"; font.pixelSize: 18; anchors.horizontalCenter: parent.horizontalCenter }
        }
    }

    Slide {
        anchors.fill: parent
        Rectangle { anchors.fill: parent; color: "#0a0b14" }
        Column {
            anchors.centerIn: parent
            spacing: 16
            Text { text: "Always-On AI Agent"; color: "#818cf8"; font.pixelSize: 28; font.bold: true; anchors.horizontalCenter: parent.horizontalCenter }
            Text { text: "Aether monitors your system 24/7, fixes issues\nautomatically, and is always one keystroke away."; color: "#94a3b8"; font.pixelSize: 16; horizontalAlignment: Text.AlignHCenter; anchors.horizontalCenter: parent.horizontalCenter }
        }
    }

    Slide {
        anchors.fill: parent
        Rectangle { anchors.fill: parent; color: "#0a0b14" }
        Column {
            anchors.centerIn: parent
            spacing: 16
            Text { text: "Access from Anywhere"; color: "#818cf8"; font.pixelSize: 28; font.bold: true; anchors.horizontalCenter: parent.horizontalCenter }
            Text { text: "Control your machine from your phone, tablet,\nor any browser with the AetherOS Remote UI."; color: "#94a3b8"; font.pixelSize: 16; horizontalAlignment: Text.AlignHCenter; anchors.horizontalCenter: parent.horizontalCenter }
        }
    }

    Slide {
        anchors.fill: parent
        Rectangle { anchors.fill: parent; color: "#0a0b14" }
        Column {
            anchors.centerIn: parent
            spacing: 16
            Text { text: "Installing AetherOS…"; color: "#818cf8"; font.pixelSize: 28; font.bold: true; anchors.horizontalCenter: parent.horizontalCenter }
            Text { text: "This takes just a few minutes. After reboot,\nrun the first-time setup to activate your AI."; color: "#94a3b8"; font.pixelSize: 16; horizontalAlignment: Text.AlignHCenter; anchors.horizontalCenter: parent.horizontalCenter }
        }
    }
}
