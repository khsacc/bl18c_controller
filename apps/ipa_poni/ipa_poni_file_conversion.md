# pyFAI (PONI) → IPAnalyzer (prm) 検出器ジオメトリ変換仕様

## 前提

BL-18C で用いている FPD では、検出器画素の歪みは非常に小さく、 CeO2 を用いた最適化でも 0.005 以下に収まることがほとんどである。 pyFAI では画素サイズおよび画素歪みを考慮しないが、BL-18Cにおいては、ほとんど問題にならないと考えられる。したがって、歪み角 $\xi=0$、`rot3` $=0$ を前提とする。

記号: pyFAI 側 $\theta_1,\theta_2=$`Rot1,Rot2`(rad), $L=$`Distance`(m), `Poni1`(m, 軸1=slow=行=縦), `Poni2`(m, 軸2=fast=列=横), `pixel1`(縦, m), `pixel2`(横, m), $\lambda=$`Wavelength`(m)。

---

## 1. 座標系の差異

| | pyFAI (PONI) | IPAnalyzer |
|---|---|---|
| 面上の基準点 | **PONI**（垂線の足, point of normal incidence） | **DirectSpot**（ダイレクトビーム位置）／ **Foot**（垂線の足）を両方保持 |
| 距離 | $L$ = サンプル→PONI の**垂直**距離 | `CameraLength2` = サンプル→Foot の垂直距離、`CameraLength1` = サンプル→DirectSpot の**ビーム沿い**距離 |
| 傾き | 順序付き3回転 $\theta_1\!\to\!\theta_2\!\to\!\theta_3$（$\theta_1,\theta_2$左手回り） | 2角 $(\varphi,\tau)$: $XY$面内で$X$から$\varphi$方向の軸まわりに$\tau$ |
| 軸 | 1=$y$(上),2=$x$(リング中心),3=$z$(ビーム) | $X$(列,右), $Y$(行,下), $Z$(ビーム) |
| 画素 | `pixel1`,`pixel2` | `pixSizeX`,`pixSizeY`, 歪み `pixKsi`（本変換で0） |

> Note: IPAnalyzer は `Foot`（＝pyFAI の PONI）と `CameraLength2`（＝pyFAI の `Distance` $L$）を明示的に保持している。傾きに依存する変換が必要なのは `DirectSpot`/`CameraLength1` と $(\varphi,\tau)$ のみ。`FootMode` フラグで基準（DirectSpot か Foot か）を切り替える設計になっている。

---

## 2. 変換式（pyFAI → IPAnalyzer）

繰り返しになるが、$\theta_3=0$ は前提とする。

### 検出器距離
$$
\texttt{CameraLength2} = L \quad(\text{m→mm}),\qquad
\texttt{CameraLength1} = \frac{L}{\cos\theta_1\cos\theta_2} \quad(\text{m→mm})
$$

### 傾き（単位：deg）
$$
\texttt{tiltTau} = \arccos(\cos\theta_1\cos\theta_2)
$$
$$
{\ \texttt{tiltPhi} = \textrm{atan2}\!\Big(\tan\theta_1,\ \dfrac{\tan\theta_2}{\cos\theta_1}\Big)\ }\quad(\text{微小角: }\textrm{atan2}(\theta_1,\theta_2))
$$

`tiltPhi` の符号は、IPAnalyzer で「Foot が DirectSpot から $(\sin\varphi,-\cos\varphi)$ 方向（$X$=列,$Y$=行）にずれる」ルールに一致する。

### 中心位置画素（原点は画像左上）
$$
\texttt{FootX}=\frac{\texttt{Poni2}}{\texttt{pixel2}},\qquad
\texttt{FootY}=\frac{\texttt{Poni1}}{\texttt{pixel1}}
$$
$$
\texttt{DirectSpotX}=\frac{\texttt{Poni2}-L\tan\theta_1}{\texttt{pixel2}},\qquad
\texttt{DirectSpotY}=\frac{\texttt{Poni1}+L\,\tan\theta_2/\cos\theta_1}{\texttt{pixel1}}
$$

### 波長（単位変換のみ）
$$
\texttt{waveLength}=\lambda\times10^{10}\ (\text{Å})
$$

### 画素サイズ（単位変換のみ）
$$
\texttt{pixSizeX}=\texttt{pixel2}\times10^{3},\quad
\texttt{pixSizeY}=\texttt{pixel1}\times10^{3}\ (\text{mm}),\quad
\texttt{pixKsi}=0
$$

---

## 3. 本アプリから生成される`.prm`ファイルへの書き込み形式

.NET XML形式に則る。ルート `<Parameter>`。

| フィールド | 型/単位 | 変換元 |
|---|---|---|
| `cameraMode` | 文字列 | `"FlatPanel"`（固定） |
| `FootMode` | Bool | `False`（DirectSpot 基準） |
| `DirectSpotX`,`DirectSpotY` | px | §2 |
| `CameraLength1` | mm | $L/(\cos\theta_1\cos\theta_2)$ |
| `FootX`,`FootY` | px | §2（PONIそのもの） |
| `CameraLength2` | mm | $L$ |
| `waveSource`,`xRayElement`,`xRayLine` | int | `0,0,0`（カスタム波長／放射光） |
| `waveLength` | Å | $\lambda\times10^{10}$ |
| `pixSizeX`,`pixSizeY` | mm | `pixel2`,`pixel1`$\times10^3$ |
| `pixKsi` | rad | `0` |
| `tiltPhi`,`tiltTau` | **deg** | §2 |
| `sphericalRadiusInverse` | — | `0` |
| `GandolfiRadius` | mm | 検出器幾何と無関係 |

---

## 4. 逆変換（IPAnalyzer → pyFAI）


$$
L=\texttt{CameraLength2},\quad \texttt{Poni1}=\texttt{FootY}\cdot\texttt{pixel1},\quad \texttt{Poni2}=\texttt{FootX}\cdot\texttt{pixel2}
$$
$$
\theta_1=\textrm{atan2}\!\big((\texttt{FootX}-\texttt{DirectSpotX})\texttt{pixel2},\,L\big),\quad
\theta_2=-\textrm{atan2}\!\big((\texttt{FootY}-\texttt{DirectSpotY})\texttt{pixel1}\cos\theta_1,\,L\big)
$$