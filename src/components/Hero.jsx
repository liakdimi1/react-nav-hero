import React from 'react';
import backgroundVideo from './video.mp4'; // Path to your video file
import "./style.css";
import logo from './logo.png';

function Hero() {
  
  
  return (
    <div className='hero-container'>
      <img src={logo} alt="Logo" className="logo" />
      <video className="background-video" autoplay muted loop>
        <source src={backgroundVideo} type="video/mp4" />
        Your browser does not support the video tag.
      </video>
      <h2 className='slogan typewriter'>
    <span className='span1'>Εμπιστοσύνη και φροντίδα για το αυτοκίνητό σας,</span>
    <br />
    <span className='span2'>από την οικογένειά μας στη δική σας.</span>
</h2>
      
    </div>
  );
}

export default Hero;
