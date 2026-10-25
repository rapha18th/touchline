"""Auto-eventing from a rendered broadcast feed.

The pipeline sees only JPEG frames and the two kit colours. It finds the pitch from its painted lines,
the ball and the players, and turns their motion into passes and shots with distances and speeds.
The engine's tracking and the renderer's camera matrix grade it afterwards.
"""

LENGTH, WIDTH, FPS = 105.0, 68.0, 25
