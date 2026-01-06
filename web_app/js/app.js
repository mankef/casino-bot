class CasinoApp {
    constructor() {
        this.tg = window.Telegram.WebApp;
        this.currentBet = 5;
        this.currentGame = 'slots';
        this.init();
    }

    init() {
        this.tg.expand();
        this.tg.enableClosingConfirmation();
        
        this.setupEventListeners();
        this.loadBalance();
    }

    setupEventListeners() {
        // Game tabs
        document.querySelectorAll('.game-tab').forEach(tab => {
            tab.addEventListener('click', (e) => {
                document.querySelectorAll('.game-tab').forEach(t => t.classList.remove('active'));
                e.target.classList.add('active');
                
                this.currentGame = e.target.dataset.game;
                document.querySelectorAll('.game-container').forEach(g => g.classList.remove('active'));
                document.getElementById(`${this.currentGame}-game`).classList.add('active');
            });
        });

        // Bet controls
        document.querySelectorAll('.bet-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.bet-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                this.currentBet = parseFloat(e.target.dataset.bet);
                this.updateBetButton();
            });
        });

        // Bottom nav
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
                item.classList.add('active');
            });
        });

        // Pull to refresh
        let startY = 0;
        document.addEventListener('touchstart', (e) => {
            startY = e.touches[0].clientY;
        });

        document.addEventListener('touchend', (e) => {
            const endY = e.changedTouches[0].clientY;
            if (endY - startY > 100 && window.scrollY === 0) {
                this.loadBalance();
                this.showToast('Баланс обновлен', 'success');
            }
        });
    }

    updateBetButton() {
        const btn = document.querySelector('.spin-btn .btn-bet');
        if (btn) btn.textContent = `${this.currentBet} USDT`;
    }

    async loadBalance() {
        try {
            const response = await fetch('/api/webapp/init', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: this.tg.initData })
            });
            
            const data = await response.json();
            
            if (data.success) {
                document.getElementById('balance').textContent = data.balance.toFixed(2);
                
                // Обновляем статистику
                if (data.stats) {
                    document.getElementById('games-count').textContent = data.stats.games;
                    document.getElementById('total-bet').textContent = data.stats.total_bet.toFixed(2);
                    document.getElementById('total-win').textContent = data.stats.total_win.toFixed(2);
                }
            }
        } catch (error) {
            this.showToast('Ошибка загрузки баланса', 'error');
            console.error('Load balance error:', error);
        }
    }

    async playSlots() {
        const button = document.querySelector('.spin-btn');
        const balanceEl = document.getElementById('balance');
        const currentBalance = parseFloat(balanceEl.textContent);
        
        if (currentBalance < this.currentBet) {
            this.showToast('Недостаточно средств!', 'error');
            return;
        }

        button.disabled = true;
        
        // Анимация прокрутки
        const reels = document.querySelectorAll('.reel');
        reels.forEach((reel, i) => {
            reel.style.animation = 'none';
            setTimeout(() => {
                reel.style.animation = `spin ${0.5 + i * 0.1}s ease-out`;
            }, 10);
        });

        try {
            const response = await fetch('/api/game/play', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    initData: this.tg.initData,
                    gameType: 'slots',
                    betAmount: this.currentBet
                })
            });

            const data = await response.json();
            
            if (data.success) {
                // Обновляем барабаны
                setTimeout(() => {
                    reels.forEach((reel, i) => {
                        reel.textContent = data.result.reels[i];
                    });
                    
                    // Обновляем баланс
                    document.getElementById('balance').textContent = data.new_balance.toFixed(2);
                    
                    // Показываем результат
                    if (data.result.is_win) {
                        const profit = (data.result.win_amount - this.currentBet).toFixed(2);
                        this.showToast(`+${profit} USDT!`, 'success');
                    } else {
                        this.showToast('Проигрыш', 'error');
                    }
                    
                    button.disabled = false;
                }, 1500);
            } else {
                this.showToast(data.error || 'Ошибка игры', 'error');
                button.disabled = false;
            }
        } catch (error) {
            this.showToast('Ошибка игры', 'error');
            button.disabled = false;
            console.error('Play error:', error);
        }
    }

    async playRoulette() {
        this.showToast('Рулетка в разработке', 'error');
        // Реализация аналогична playSlots
    }

    showTab(tabName) {
        // Скрываем все контенты
        document.querySelector('.main-content').style.display = tabName === 'games' ? 'block' : 'none';
        document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
        
        // Показываем нужный
        if (tabName !== 'games') {
            document.getElementById(`${tabName}-tab`).classList.add('active');
        }
    }

    showToast(message, type = 'info') {
        const toast = document.getElementById('toast');
        toast.textContent = message;
        toast.className = `toast ${type} show`;
        
        setTimeout(() => {
            toast.classList.remove('show');
        }, 3000);
    }
}

// Инициализация
const app = new CasinoApp();