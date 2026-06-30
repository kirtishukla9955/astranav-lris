import matplotlib.pyplot as plt
import numpy as np

def plot_bezier(p0, p1, p2, p3, label):
    t = np.linspace(0, 1, 100)
    x = (1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0] + 3*(1-t)*t**2*p2[0] + t**3*p3[0]
    y = (1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1] + 3*(1-t)*t**2*p2[1] + t**3*p3[1]
    plt.plot(x, y, label=label)

plt.figure(figsize=(10, 10))
plot_bezier([150,80], [150,140], [150,150], [230,200], 'Path 1 (Shackleton)')
plot_bezier([430,85], [430,140], [430,150], [350,205], 'Path 2 (Ice Volume)')
plot_bezier([120,360], [120,300], [120,290], [190,260], 'Path 3 (Temp)')
plot_bezier([460,355], [460,300], [460,290], [380,265], 'Path 4 (LMRS)')

plt.gca().invert_yaxis() # SVG coordinates
plt.legend()
plt.savefig('curves.png')
