import numpy as np
import matplotlib.pyplot as plt

# Second harmonic simulation of WMS using Schilt's mathematic derivation

# 参数设置
x = np.arange(-10, 10.01, 0.01)  # normalized frequency x = (v-vline)/dvline
dv = 2.2          # laser frequency deviation
dvline = 2        # HWHM of absorption profile
I0 = 1            # input power
a0 = 1            # absorpance
psi = np.pi       # IM-FM phase
phi = 2 * psi     # Lockin phase
Iomega = 1        # low frequency ramp power
pw = 0.5          # modulation frequency power variation

# 计算中间变量
m = dv / dvline
X = 1 - x**2 + m**2
r = np.sqrt(X**2 + 4 * x**2)

# Schilt 推导的 s1, s2, s3
s1 = I0 * a0 * ((np.sqrt(2) / m) * ((-x) * np.sqrt(r + X) + np.sign(x) * np.sqrt(r - X)) / r)
s2 = I0 * a0 * (-4 / m**2 + (np.sqrt(2) / m**2) * ((r + 1 - x**2) * np.sqrt(r + X) + 2 * np.abs(x) * np.sqrt(r - X)) / r)
s3 = (-I0 * a0 / m**3) * (16 * x + (np.sqrt(2) / r) * (x**3 - 3 * x * (r + 1)) * np.sqrt(r + X) +
                          (np.sqrt(2) / r) * np.sign(x) * (1 - 3 * x**2 - 3 * r) * np.sqrt(r - X))

# 二次谐波分量
s2p = Iomega * np.cos(2 * psi * s2) - pw * dvline * (m / 2) * (np.cos(psi * s1) + np.cos(3 * psi * s3))
s2q = Iomega * np.sin(2 * psi * s2) - pw * dvline * (m / 2) * (np.sin(psi * s1) + np.sin(3 * psi * s3))

# 锁相放大器输出
s2phi = -(s2p * np.cos(phi) + s2q * np.sin(phi))

# 绘图
plt.figure(figsize=(8, 5))
plt.plot(x, s2phi)
plt.xlabel('Normalized Frequency x')
plt.ylabel('2f Signal')
plt.title('Second Harmonic Simulation of WMS (Schilt\'s Derivation)')
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('wms_2f_signal.png', dpi=150)
plt.show()
