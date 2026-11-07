import AppKit
// 生成 1024pt 图标：深蓝底 + 声波柱 + 圆角
let size = 1024.0
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

let rect = CGRect(x: 0, y: 0, width: size, height: size)
let path = CGPath(roundedRect: rect.insetBy(dx: 12, dy: 12), cornerWidth: 220, cornerHeight: 220, transform: nil)
ctx.saveGState()
ctx.addPath(path); ctx.clip()
let c1 = NSColor(srgbRed: 0.29, green: 0.40, blue: 0.86, alpha: 1).cgColor
let c2 = NSColor(srgbRed: 0.16, green: 0.22, blue: 0.52, alpha: 1).cgColor
let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [c1, c2] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: size), end: CGPoint(x: size, y: 0), options: [])

// 声波柱
let heights: [CGFloat] = [0.20, 0.36, 0.55, 0.74, 0.55, 0.36, 0.20]
let barW: CGFloat = 62, gap: CGFloat = 44
let total = CGFloat(heights.count) * barW + CGFloat(heights.count - 1) * gap
var x = (size - total) / 2
ctx.setFillColor(NSColor.white.withAlphaComponent(0.95).cgColor)
for h in heights {
    let bh = h * size
    let r = CGRect(x: x, y: (size - bh) / 2, width: barW, height: bh)
    ctx.addPath(CGPath(roundedRect: r, cornerWidth: barW / 2, cornerHeight: barW / 2, transform: nil))
    ctx.fillPath()
    x += barW + gap
}
ctx.restoreGState()

// 高光
ctx.saveGState()
ctx.addPath(path); ctx.clip()
let shine = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                       colors: [NSColor.white.withAlphaComponent(0.18).cgColor,
                                NSColor.white.withAlphaComponent(0).cgColor] as CFArray,
                       locations: [0, 1])!
ctx.drawLinearGradient(shine, start: CGPoint(x: 0, y: size), end: CGPoint(x: 0, y: size * 0.55), options: [])
ctx.restoreGState()

img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
