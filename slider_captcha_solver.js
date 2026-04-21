
/**
 * 滑块验证码解决器 - 注入到浏览器中
 *
 * 使用方法：
 * 1. 找到滑块元素和目标位置
 * 2. 调用 dragSlider(sliderElement, targetOffset)
 */

function generateHumanLikePath(startX, startY, endX, endY, pointCount = 120, overshootChance = 0.3) {
    const totalDistance = endX - startX;
    const hasOvershoot = Math.random() < overshootChance;
    const overshootDistance = hasOvershoot ? (30 + Math.random() * 50) : 0;
    const maxOvershootX = endX + overshootDistance;
    
    const path = [];
    
    // 阶段1：加速到最大速度（0% → 25%）
    const phase1Count = Math.floor(pointCount * 0.25);
    for (let i = 0; i < phase1Count; i++) {
        const t = i / phase1Count;
        const x = startX + totalDistance * 0.6 * t;
        const y = startY + (Math.random() - 0.5) * 1.5;
        const speedFactor = 0.2 + 3.8 * t;
        
        path.push({ x, y, speedFactor });
    }
    
    // 阶段2：继续前进但减速（25% → 70%）
    const phase2Count = Math.floor(pointCount * 0.45);
    for (let i = 0; i < phase2Count; i++) {
        const t = i / phase2Count;
        const baseX = startX + totalDistance * 0.6;
        const targetX = hasOvershoot ? maxOvershootX : endX;
        const x = baseX + (targetX - baseX) * t;
        const y = startY + (Math.random() - 0.5) * 1.5;
        const speedFactor = 4.0 - 3.0 * t;
        
        path.push({ x, y, speedFactor });
    }
    
    // 阶段3（有过冲）：反向减速回到终点（70% → 100%）
    if (hasOvershoot) {
        const phase3Count = pointCount - phase1Count - phase2Count;
        for (let i = 0; i < phase3Count; i++) {
            const t = i / phase3Count;
            const x = maxOvershootX - overshootDistance * t;
            const y = startY + (Math.random() - 0.5) * 1.5;
            const speedFactor = 1.0 - 0.9 * t;
            
            path.push({ x, y, speedFactor });
        }
    } else {
        const phase3Count = pointCount - phase1Count - phase2Count;
        for (let i = 0; i < phase3Count; i++) {
            const t = i / phase3Count;
            const x = endX;
            const y = startY + (Math.random() - 0.5) * 1.5;
            const speedFactor = 1.0 - 0.9 * t;
            
            path.push({ x, y, speedFactor });
        }
    }
    
    return path;
}

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function dragSlider(sliderElement, targetOffset = null, pointCount = 120) {
    const box = sliderElement.getBoundingClientRect();
    const startX = box.left + box.width / 2;
    const startY = box.top + box.height / 2;
    
    let endX;
    if (targetOffset) {
        endX = startX + targetOffset;
    } else {
        const track = document.querySelector('.slider-track, [class*="track"], [class*="slider"]');
        if (track) {
            const trackBox = track.getBoundingClientRect();
            endX = trackBox.left + trackBox.width - box.width / 2;
        } else {
            endX = startX + 280;
        }
    }
    
    const path = generateHumanLikePath(startX, startY, endX, startY, pointCount);
    
    const pressEvent = new MouseEvent('mousedown', {
        clientX: startX, clientY: startY,
        button: 0, bubbles: true, cancelable: true
    });
    sliderElement.dispatchEvent(pressEvent);
    
    await sleep(50 + Math.random() * 100);
    
    for (const point of path) {
        const baseDelay = 25;
        const delay = baseDelay / point.speedFactor;
        
        const moveEvent = new MouseEvent('mousemove', {
            clientX: point.x, clientY: point.y,
            bubbles: true, cancelable: true
        });
        document.dispatchEvent(moveEvent);
        
        await sleep(delay);
    }
    
    await sleep(150 + Math.random() * 200);
    
    const releaseEvent = new MouseEvent('mouseup', {
        clientX: endX, clientY: startY,
        button: 0, bubbles: true, cancelable: true
    });
    sliderElement.dispatchEvent(releaseEvent);
    
    return true;
}

window.dragSlider = dragSlider;
window.generateHumanLikePath = generateHumanLikePath;
console.log('Slider captcha solver loaded! Use window.dragSlider(sliderElement, targetOffset)');

