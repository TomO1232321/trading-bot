"""Macht aus Text eine einzeilige GitHub-Annotation (Zeilenumbrueche kodiert)."""
import sys

text = sys.stdin.read().strip() or "(keine Ausgabe)"
print(text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
